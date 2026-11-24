"""店铺事实（Step 6）：可查、可改、可删，冲突不静默覆盖。

存储：``data/tenants/<tid>/memory/facts.jsonl``，一行一个事件，只追加：

    {"op": "add",    "memory_id": "M-0001", "scope": "shop", "type": "rule",
     "content": "不做宠物类目", "source": "user_instruction", ...}
    {"op": "status", "memory_id": "M-0001", "status": "superseded",
     "superseded_by": "M-0002", "at": "2026-11-05T10:00:00"}

为什么是事件行而不是原地改写：PRD 同时要求"旧记录标 superseded"和"append-only
不允许改写历史行"。两个要求只有一种同时成立的写法——状态也当成一条新事件追加，
读取时按 ``memory_id`` 折叠：内容取 ``add`` 那条，状态取最后一条事件。

和 ``store.py`` 的四个文件刻意分开。那边是主管的作业记忆（画像/实验/决策/经验），
这边是可以被检索、注入后续任务的店铺事实。**委托原文不进这里**：``intake/history``
是回看用的流水，facts 只收白名单来源。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from ..config import default_data_dir

SCHEMA_VERSION = 1
FACTS_FILENAME = "facts.jsonl"

#: 来源白名单 + 每个来源的置信度上限。不在表里的来源一律拒绝写入——这是
#: "模型不能自己决定记什么"的唯一硬约束：没有代码路径给它一个合法 source。
SOURCE_CONFIDENCE: dict[str, float] = {
    "user_instruction": 1.0,
    "draft_adopted": 0.9,
    "manual_edit": 0.8,
    "pattern": 0.7,
}

SOURCE_LABELS = {
    "user_instruction": "用户明说",
    "draft_adopted": "采纳草稿",
    "manual_edit": "人工修改",
    "pattern": "观察规律",
}

SCOPES = ("shop", "product", "customer", "market")
SCOPE_LABELS = {
    "shop": "店铺",
    "product": "商品",
    "customer": "客户",
    "market": "市场",
}

TYPES = ("rule", "preference", "constraint", "fact")
TYPE_LABELS = {
    "rule": "规则",
    "preference": "偏好",
    "constraint": "约束",
    "fact": "事实",
}

ACTIVE = "active"
SUPERSEDED = "superseded"
REVOKED = "revoked"
EXPIRED = "expired"

#: 一条事实的字数上限。委托原文动辄几十上百字，这个上限本身就挡住"把一句话
#: 委托整段灌进 facts"的做法。
MAX_CONTENT = 400
MAX_REASON = 200
DEFAULT_IMPORTANCE = 0.6

EXPIRE_AFTER_DAYS = 180
EXPIRE_BELOW_IMPORTANCE = 0.5
#: 打分用的时间衰减半衰期。比 180 天的"过期线"短，是因为排序要更敏感：
#: 同样重要的一条旧规则可以留，但不该压过上周刚说的那条。
DECAY_HALF_LIFE_DAYS = 120

#: 冲突判定：一条带否定一条不带，且说的是同一件事。
#: "同一件事"先看最长公共子串（≥4 字），短句再看字符二元组重合度——
#: 只看 Jaccard 会被"…：利润太薄且退货率高"这种附加从句稀释，
#: "不做宠物类目：利润太薄" 与 "可以做宠物类目：找到供应商了" 反而判不出来。
CONFLICT_LCS_CHARS = 4
CONFLICT_SIMILARITY = 0.5
CONFLICT_MIN_OVERLAP = 3

_NEGATIONS = (
    "不做", "不要", "不用", "不支持", "不能", "不允许", "不得", "禁止", "避免",
    "别做", "别用", "不考虑", "不接", "不卖", "不上", "不发", "不写", "无需",
    "never", "avoid", "don't", "do not", "not allowed",
)
_PUNCT = re.compile(r"[\s，。、；：！？,.;:!?·\-—_/\\()（）\[\]【】\"'“”‘’]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[A-Za-z0-9]+")
_ID_SEQ = re.compile(r"^M-(\d+)$")


# ------------------------------------------------------------------ helpers


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse(stamp) -> datetime | None:
    try:
        return datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None


def _age_days(stamp, now: datetime) -> float:
    then = _parse(stamp)
    if then is None:
        return 0.0
    return max(0.0, (now - then).total_seconds() / 86400.0)


def _normalize(text) -> str:
    return _PUNCT.sub("", str(text or "").lower())


def _bigrams(text) -> set[str]:
    raw = _normalize(text)
    if len(raw) < 2:
        return {raw} if raw else set()
    return {raw[index : index + 2] for index in range(len(raw) - 1)}


def _negated(text) -> bool:
    raw = str(text or "").lower()
    return any(marker in raw for marker in _NEGATIONS)


def _longest_common_substring(left: str, right: str) -> int:
    """最长公共子串长度。内容上限 400 字，O(n·m) 足够快。"""

    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    best = 0
    for char in left:
        current = [0] * (len(right) + 1)
        for index, other in enumerate(right, start=1):
            if char == other:
                current[index] = previous[index - 1] + 1
                if current[index] > best:
                    best = current[index]
        previous = current
    return best


def _contradicts(old: str, new: str) -> bool:
    """两条同 scope、同 type 的事实是不是互相矛盾。

    只认一种形态：说的是同一件事，但一条否定一条肯定。
    "不做宠物类目" vs "可以做宠物类目" 是矛盾；"不做宠物类目" vs "不做 3C 类目"
    不是。判不出来的（同义改写、跨语言）交给调用方显式传 ``supersedes``。
    """

    left, right = _normalize(old), _normalize(new)
    if not left or not right or left == right:
        return False
    if _negated(old) == _negated(new):
        return False
    if _longest_common_substring(left, right) >= CONFLICT_LCS_CHARS:
        return True
    shared = _bigrams(old) & _bigrams(new)
    if len(shared) < CONFLICT_MIN_OVERLAP:
        return False
    union = _bigrams(old) | _bigrams(new)
    return bool(union) and len(shared) / len(union) >= CONFLICT_SIMILARITY


def estimate_tokens(text) -> int:
    """粗估 token 数：中文按字、英文按词。只用于预算截断，不需要精确。"""

    raw = str(text or "")
    return max(1, len(_CJK.findall(raw)) + len(_WORD.findall(raw)))


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def format_injection(records: list[dict]) -> str:
    """把检索结果拼成可注入提示词的一段文字。没有记录时返回空串。"""

    lines = []
    for record in records or []:
        content = str(record.get("content") or "").strip()
        if not content:
            continue
        scope = str(record.get("scope") or "shop")
        lines.append(f"- [{SCOPE_LABELS.get(scope, scope)}] {content}")
    return "\n".join(lines)


# -------------------------------------------------------------------- store


class FactsStore:
    """一个租户的 facts.jsonl。所有写操作都是追加一行，读操作只读。"""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else default_data_dir() / "memory"
        self.bad_lines = 0
        self._bad_keys: set[tuple[str, str]] = set()

    @property
    def path(self) -> Path:
        return self.root / FACTS_FILENAME

    # ------------------------------------------------------------- reading

    def _events(self) -> list[dict]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        events: list[dict] = []
        for number, line in enumerate(raw.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                key = (str(self.path), str(number))
                if key not in self._bad_keys:
                    self._bad_keys.add(key)
                    self.bad_lines += 1
                continue
            if isinstance(payload, dict) and payload.get("memory_id"):
                events.append(payload)
        return events

    def _records(self, now: datetime | None = None) -> dict[str, dict]:
        clock = now or datetime.now()
        records: dict[str, dict] = {}
        for event in self._events():
            memory_id = str(event.get("memory_id"))
            op = str(event.get("op") or "add")
            if op == "add" and memory_id not in records:
                records[memory_id] = {
                    "memory_id": memory_id,
                    "scope": str(event.get("scope") or "shop"),
                    "type": str(event.get("type") or "fact"),
                    "content": str(event.get("content") or ""),
                    "source": str(event.get("source") or ""),
                    "confidence": _as_float(event.get("confidence"), 0.0),
                    "importance": _as_float(event.get("importance"), DEFAULT_IMPORTANCE),
                    "created_at": str(event.get("created_at") or ""),
                    "last_used_at": event.get("last_used_at"),
                    "status": str(event.get("status") or ACTIVE),
                    "supersedes": event.get("supersedes"),
                    "superseded_by": event.get("superseded_by"),
                    "revoked_at": event.get("revoked_at"),
                    "revoke_reason": event.get("revoke_reason"),
                    "expired_at": event.get("expired_at"),
                    "actor": str(event.get("actor") or ""),
                    "schema": int(event.get("schema") or SCHEMA_VERSION),
                }
                continue
            record = records.get(memory_id)
            if record is None:
                continue
            if op == "status":
                status = str(event.get("status") or record["status"])
                record["status"] = status
                for key in ("superseded_by", "revoked_at", "revoke_reason", "expired_at"):
                    if event.get(key):
                        record[key] = event[key]
            elif op == "touch":
                record["last_used_at"] = str(event.get("at") or "") or record["last_used_at"]
        # 补充推导：新记录声明了 supersedes，即使状态事件丢了也能折叠正确。
        for record in records.values():
            target = record.get("supersedes")
            if record.get("status") != ACTIVE or not target:
                continue
            old = records.get(str(target))
            if old is not None and old.get("status") == ACTIVE:
                old["status"] = SUPERSEDED
                old["superseded_by"] = old.get("superseded_by") or record["memory_id"]
        for record in records.values():
            record["effective_status"] = _effective_status(record, clock)
        return records

    def list(
        self,
        scope: str | None = None,
        status: str = ACTIVE,
        fact_type: str | None = None,
        limit: int = 50,
        now: datetime | None = None,
    ) -> list[dict]:
        """按 scope/type/状态过滤，新的在前。``status="all"`` 返回全部。"""

        records = self._records(now=now)
        scope = (scope or "").strip().lower()
        fact_type = (fact_type or "").strip().lower()
        wanted = (status or ACTIVE).strip().lower()
        items = []
        for record in records.values():
            if scope and record["scope"] != scope:
                continue
            if fact_type and record["type"] != fact_type:
                continue
            current = record["effective_status"]
            if wanted != "all" and current != wanted:
                continue
            items.append(record)
        items.sort(key=lambda item: (str(item.get("created_at") or ""), item["memory_id"]), reverse=True)
        return items[: max(0, int(limit))] if limit else items

    def get(self, memory_id: str, now: datetime | None = None) -> dict | None:
        return self._records(now=now).get(str(memory_id or ""))

    def stats(self, now: datetime | None = None) -> dict:
        records = self._records(now=now)
        counts = {ACTIVE: 0, SUPERSEDED: 0, REVOKED: 0, EXPIRED: 0}
        for record in records.values():
            key = record["effective_status"]
            counts[key] = counts.get(key, 0) + 1
        return {
            "total": len(records),
            "by_status": counts,
            "bad_lines": self.bad_lines,
            "path": str(self.path),
        }

    # ------------------------------------------------------------- writing

    def add(
        self,
        *,
        scope: str,
        fact_type: str,
        content: str,
        source: str,
        importance: float = DEFAULT_IMPORTANCE,
        confidence: float | None = None,
        supersedes: str | None = None,
        actor: str = "",
        now: datetime | None = None,
    ) -> dict:
        """写一条事实。返回 ``{"fact", "duplicate", "superseded"}``。

        三种结局：
        * 内容与现有 active 记录一模一样 → 不写，``duplicate=True``；
        * 与同 scope+type 的 active 记录矛盾 → 新记录带 ``supersedes``，
          旧记录追加一条 ``status=superseded`` 事件（历史行不动）；
        * 其余情况就是普通追加。
        """

        scope = str(scope or "").strip().lower()
        if scope not in SCOPES:
            raise ValueError(f"未知的 scope：{scope or '(空)'}；只接受 {', '.join(SCOPES)}")
        fact_type = str(fact_type or "").strip().lower()
        if fact_type not in TYPES:
            raise ValueError(f"未知的 type：{fact_type or '(空)'}；只接受 {', '.join(TYPES)}")
        content = str(content or "").strip()
        if not content:
            raise ValueError("内容不能为空")
        if len(content) > MAX_CONTENT:
            raise ValueError(f"内容过长（上限 {MAX_CONTENT} 字，实际 {len(content)} 字）")
        source = str(source or "").strip().lower()
        if source not in SOURCE_CONFIDENCE:
            raise ValueError(
                "来源不在白名单："
                f"{source or '(空)'}；只接受 {', '.join(SOURCE_CONFIDENCE)}"
            )

        ceiling = SOURCE_CONFIDENCE[source]
        # 置信度只能被调低，不能被调高：pattern 观察来的东西不能自称 1.0。
        claimed = ceiling if confidence is None else _as_float(confidence, ceiling)
        confidence = _clamp(min(claimed, ceiling))
        importance = _clamp(_as_float(importance, DEFAULT_IMPORTANCE))

        clock = now or datetime.now()
        stamp = clock.replace(microsecond=0).isoformat(timespec="seconds")
        records = self._records(now=clock)

        for record in records.values():
            if record["effective_status"] != ACTIVE:
                continue
            if record["scope"] != scope or record["type"] != fact_type:
                continue
            if _normalize(record["content"]) == _normalize(content):
                return {"fact": record, "duplicate": True, "superseded": []}

        targets: list[dict] = []
        if supersedes:
            old = records.get(str(supersedes))
            if old is None:
                raise ValueError(f"找不到要替代的事实：{supersedes}")
            if old["effective_status"] != ACTIVE:
                raise ValueError(f"事实 {supersedes} 已经不是 active，无法被替代")
            if old["scope"] != scope:
                raise ValueError("被替代的事实必须与新事实同 scope")
            targets = [old]
        else:
            for record in records.values():
                if record["effective_status"] != ACTIVE:
                    continue
                if record["scope"] != scope or record["type"] != fact_type:
                    continue
                if _contradicts(record["content"], content):
                    targets.append(record)
            targets.sort(key=lambda item: str(item.get("created_at") or ""))

        memory_id = self._next_id(records)
        fact = {
            "schema": SCHEMA_VERSION,
            "op": "add",
            "memory_id": memory_id,
            "scope": scope,
            "type": fact_type,
            "content": content,
            "source": source,
            "confidence": round(confidence, 2),
            "importance": round(importance, 2),
            "created_at": stamp,
            "last_used_at": None,
            "status": ACTIVE,
            "supersedes": targets[-1]["memory_id"] if targets else None,
            "superseded_by": None,
            "actor": str(actor or ""),
        }
        self._append(fact)
        for old in targets:
            self._append(
                {
                    "op": "status",
                    "memory_id": old["memory_id"],
                    "status": SUPERSEDED,
                    "superseded_by": memory_id,
                    "at": stamp,
                    "actor": str(actor or ""),
                }
            )
            old["status"] = SUPERSEDED
            old["superseded_by"] = memory_id
            old["effective_status"] = SUPERSEDED
        stored = dict(fact)
        stored["effective_status"] = ACTIVE
        return {"fact": stored, "duplicate": False, "superseded": [old["memory_id"] for old in targets]}

    def revoke(self, memory_id: str, reason: str = "", actor: str = "", now: datetime | None = None) -> dict:
        """软删：追加一条 ``status=revoked`` 事件，历史行原样保留。"""

        records = self._records(now=now)
        record = records.get(str(memory_id or ""))
        if record is None:
            raise KeyError(memory_id)
        if record["effective_status"] == REVOKED:
            return {"fact": record, "changed": False}
        stamp = (now or datetime.now()).replace(microsecond=0).isoformat(timespec="seconds")
        self._append(
            {
                "op": "status",
                "memory_id": record["memory_id"],
                "status": REVOKED,
                "revoked_at": stamp,
                "revoke_reason": str(reason or "")[:MAX_REASON],
                "at": stamp,
                "actor": str(actor or ""),
            }
        )
        record["status"] = REVOKED
        record["effective_status"] = REVOKED
        record["revoked_at"] = stamp
        record["revoke_reason"] = str(reason or "")[:MAX_REASON]
        return {"fact": record, "changed": True}

    def touch(self, memory_id: str, now: datetime | None = None) -> bool:
        """记一次"被用到"。检索本身不写盘，调用方显式决定要不要留痕。"""

        records = self._records(now=now)
        record = records.get(str(memory_id or ""))
        # 这里看的是记录状态而不是推导状态：一条已经"算作过期"的事实被用到，
        # 应该被救回来，而不是连 touch 都不让。
        if record is None or record["status"] != ACTIVE:
            return False
        stamp = (now or datetime.now()).replace(microsecond=0).isoformat(timespec="seconds")
        self._append({"op": "touch", "memory_id": record["memory_id"], "at": stamp})
        return True

    def expire_stale(self, now: datetime | None = None) -> list[str]:
        """把"太久没用 + 不重要"的 active 记录标成 expired。返回被标的 id。"""

        clock = now or datetime.now()
        stamp = clock.replace(microsecond=0).isoformat(timespec="seconds")
        expired: list[str] = []
        for record in self._records(now=clock).values():
            # 只在"状态还是 active、但已经算过期"时补一条状态事件；已经标过的跳过。
            if record["status"] != ACTIVE or record["effective_status"] != EXPIRED:
                continue
            self._append(
                {
                    "op": "status",
                    "memory_id": record["memory_id"],
                    "status": EXPIRED,
                    "expired_at": stamp,
                    "at": stamp,
                }
            )
            expired.append(record["memory_id"])
        expired.sort()
        return expired

    # -------------------------------------------------------------- search

    def search(
        self,
        scope: str | None = None,
        query: str = "",
        budget_tokens: int = 800,
        limit: int = 20,
        now: datetime | None = None,
    ) -> dict:
        """结构化过滤 → 关键词打分 → 预算截断。

        打分 = 相关性 × importance × 时间衰减。只召回 active（过期/被替代/
        已撤回的一律不出现），且**不在检索时写盘**。
        """

        clock = now or datetime.now()
        budget = max(1, int(budget_tokens))
        grams = _bigrams(query) if str(query or "").strip() else set()
        scope = (scope or "").strip().lower()

        scored: list[tuple[float, float, dict]] = []
        for record in self._records(now=clock).values():
            if record["effective_status"] != ACTIVE:
                continue
            if scope and record["scope"] != scope:
                continue
            if grams:
                shared = grams & _bigrams(record["content"])
                if not shared:
                    continue
                relevance = len(shared) / len(grams)
            else:
                relevance = 1.0
            age = _age_days(record.get("last_used_at") or record.get("created_at"), clock)
            decay = 0.5 ** (age / DECAY_HALF_LIFE_DAYS)
            score = relevance * float(record["importance"]) * decay
            scored.append((score, relevance, record))
        scored.sort(key=lambda item: (-item[0], item[2]["memory_id"]))

        candidates = scored[: max(1, int(limit))]
        picked: list[dict] = []
        used = 0
        truncated = False
        for index, (score, relevance, record) in enumerate(candidates):
            cost = estimate_tokens(record["content"])
            if picked and used + cost > budget:
                truncated = True
                break
            picked.append(
                {
                    **record,
                    "score": round(score, 4),
                    "relevance": round(relevance, 4),
                    "tokens": cost,
                }
            )
            used += cost
            if used >= budget:
                truncated = index < len(candidates) - 1
                break
        return {
            "facts": picked,
            "count": len(picked),
            "candidates": len(scored),
            "tokens": used,
            "budget_tokens": budget,
            "truncated": truncated,
        }

    # ------------------------------------------------------------- helpers

    def _next_id(self, records: dict[str, dict]) -> str:
        highest = 0
        for memory_id in records:
            match = _ID_SEQ.match(memory_id)
            if match:
                highest = max(highest, int(match.group(1)))
        candidate = f"M-{highest + 1:04d}"
        while candidate in records:  # 理论上不会发生，防手改文件造成的空洞
            highest += 1
            candidate = f"M-{highest + 1:04d}"
        return candidate

    def _append(self, event: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        prefix = ""
        try:
            existing = self.path.read_text(encoding="utf-8")
            if existing and not existing.endswith("\n"):
                prefix = "\n"
        except OSError:
            existing = ""
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(prefix + json.dumps(event, ensure_ascii=False) + "\n")


def _effective_status(record: dict, now: datetime) -> str:
    """把"过期"算成一个推导状态，而不是等后台任务来标。

    这样即使 ``expire_stale`` 从没跑过，检索也不会召回一条 200 天前、
    重要性 0.3 的旧规则——"过期不召回"不能依赖定时任务。
    """

    status = str(record.get("status") or ACTIVE)
    if status != ACTIVE:
        return status
    if float(record.get("importance") or 0.0) >= EXPIRE_BELOW_IMPORTANCE:
        return ACTIVE
    stamp = record.get("last_used_at") or record.get("created_at")
    if _age_days(stamp, now) > EXPIRE_AFTER_DAYS:
        return EXPIRED
    return ACTIVE
