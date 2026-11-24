"""委托历史的独立落盘（append-only，best-effort）。

为什么单独一个文件，而不是塞进 ``memory/facts.jsonl``：

- **语义不同。** facts 是"店铺/客户的事实"，会被检索、注入到后续任务里；
  委托历史是"谁在什么时候说了什么"，只用于回看与审计。混在一起，一条"上这 20 个
  新品"会被当成店铺事实召回，既没用又污染上下文。
- **写入纪律不同。** facts 有来源白名单（``user_instruction`` / ``draft_adopted`` /
  ``manual_edit`` / ``pattern``），委托原文不属于任何一类，本来就不该进 facts。
- **生命周期不同。** facts 会 superseded / expired；历史只增不改，永不参与检索。

所以这里只做三件事：append 一行、读回最近 N 行、失败就算了。**没有任何异常会
冒泡到调用方**——记录历史失败不该让用户看不到自己的委托预览。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

# 租户数据目录（data/tenants/<tid>）下的相对路径。
HISTORY_SUBDIR = "intake"
HISTORY_FILENAME = "history.jsonl"

# 与 console 的委托长度上限一致；超长直接截断并留标记，避免单行 JSON 无限膨胀。
MAX_TEXT = 2000

# 一次最多读回多少条：前端只展示最近若干条，全量读回没有意义。
DEFAULT_READ_LIMIT = 50
MAX_READ_LIMIT = 200

SCHEMA_VERSION = 1


def history_path(root: Path) -> Path:
    """``data/tenants/<tid>/intake/history.jsonl``。"""

    return Path(root) / HISTORY_SUBDIR / HISTORY_FILENAME


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _mapping(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if item is not None]


def append_history(root: Path, record: dict, *, now: datetime | None = None) -> dict | None:
    """追加一条委托历史。成功返回落盘后的记录，失败返回 ``None``（不抛异常）。

    只增不改：文件永远以 ``"a"`` 打开，一行业务一行 JSON，历史行不会被重写。
    """

    if not isinstance(record, dict):
        return None
    try:
        text = _text(record.get("text"))
        created_at = (now or datetime.now()).isoformat(timespec="seconds")
        entry = {
            "schema": SCHEMA_VERSION,
            "history_id": f"H-{created_at.replace('-', '').replace(':', '')}-{uuid.uuid4().hex[:6]}",
            "created_at": created_at,
            "tenant_id": _text(record.get("tenant_id")),
            "actor": _text(record.get("actor")),
            "role": _text(record.get("role")),
            "text": text[:MAX_TEXT],
            "text_truncated": len(text) > MAX_TEXT,
            "intent": _text(record.get("intent")),
            "confidence": round(_num(record.get("confidence")), 2),
            "artifact": _text(record.get("artifact")),
            "risk_ceiling": _text(record.get("risk_ceiling")),
            "slots": _mapping(record.get("slots")),
            "missing": _text_list(record.get("missing")),
            "assumed": _mapping(record.get("assumed")),
            "pending_input": _text_list(record.get("pending_input")),
            "order_count": _int(record.get("order_count")),
            "task_ids": _text_list(record.get("task_ids")),
            "reason": _text(record.get("reason")),
            "status": "preview",
        }
        path = history_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return entry
    except (OSError, TypeError, ValueError):
        # best-effort：磁盘满、目录只读、字段不可序列化——都不该让预览失败。
        return None


def read_history(root: Path, limit: int = DEFAULT_READ_LIMIT) -> list[dict]:
    """读回最近 ``limit`` 条，新的在前。文件不存在或坏行一律跳过。"""

    try:
        limit = max(0, min(int(limit), MAX_READ_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_READ_LIMIT
    if limit == 0:
        return []
    path = history_path(root)
    if not path.exists():
        return []
    entries: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item: Any = json.loads(line)
                except ValueError:
                    continue  # 半行（进程被杀）不该让整个历史读不出来
                if isinstance(item, dict):
                    entries.append(item)
    except OSError:
        return []
    return entries[-limit:][::-1]
