"""Evidence ledger: every tool result becomes a citation the model can point at.

The id space belongs to the code, not to the model. A reply may say "obs-3",
but it cannot mint one, so every cited observation maps back to a real tool
call.

Two representations live side by side:

* **In memory** the full result is kept for the current run, because the model
  may need to quote a field that did not fit in the preview.
* **On disk** only a compact record survives: ids, digests, a 500-char summary
  and the numbers that were in the payload. The ledger is for traceability, not
  a second copy of merchant data.

Tools are expected to shape their own payloads (row limits, field whitelists,
per-item text caps) so that a normal result fits in 1500 characters. The
shaping below is the backstop for the tool that forgot to.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import OrderedDict

# A single observation is capped at 1500 characters. A category listing or a
# review batch can run to tens of kilobytes; feeding all of it back every turn
# burns the context window for facts the model already used once.
MAX_RESULT_CHARS = 1500
# First 1200 characters of the payload survive as a preview, then shrink only
# if JSON escaping would push the envelope back over the cap.
MAX_PREVIEW_CHARS = 1200
MAX_SUMMARY_CHARS = 500
MAX_NUMERIC_FACTS = 20
TRUNCATION_HINT = "narrow the query or ask for fewer fields"


def digest_arguments(arguments: dict) -> str:
    """Stable fingerprint of a call's arguments (key order must not matter)."""
    payload = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _numeric_facts(payload, limit: int = MAX_NUMERIC_FACTS) -> list:
    """Numbers in the payload, in reading order, de-duplicated.

    The compaction step keeps these so a later turn can still say "ctr 1.8"
    even after the raw JSON has been replaced by a one-liner.
    """
    found: list = []

    def walk(value) -> None:
        if len(found) >= limit:
            return
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            number = round(value, 4) if isinstance(value, float) else value
            if number not in found:
                found.append(number)
            return
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(payload)
    return found[:limit]


def _summarise(tool: str, result: dict) -> str:
    data = result.get("data")
    fields = list(data)[:8] if isinstance(data, dict) else []
    parts = [f"{tool}: ok={bool(result.get('ok'))}"]
    if result.get("code"):
        parts.append(f"code={result['code']}")
    if fields:
        parts.append("fields=" + ",".join(str(name) for name in fields))
    facts = _numeric_facts(result, limit=6)
    if facts:
        parts.append("numbers=" + ",".join(str(value) for value in facts))
    text = " | ".join(parts)
    return text[: MAX_SUMMARY_CHARS - 3] + "..." if len(text) > MAX_SUMMARY_CHARS else text


def _dropped_fields(data) -> list[str]:
    """Largest fields, so the model knows what it is missing and can narrow."""
    if not isinstance(data, dict) or not data:
        return ["data"]
    sizes = sorted(
        ((str(key), len(json.dumps(value, ensure_ascii=False, default=str))) for key, value in data.items()),
        key=lambda pair: pair[1],
        reverse=True,
    )
    dropped = [name for name, size in sizes[:3] if size >= 200]
    return dropped or ["data"]


def _truncate(result: dict) -> dict:
    """Keep the envelope a dict: preview + what was dropped + how to narrow."""
    data = result.get("data")
    data_text = json.dumps(data, ensure_ascii=False, default=str)
    preview = data_text[:MAX_PREVIEW_CHARS]
    envelope = {
        "truncated": True,
        "preview": preview,
        "dropped": _dropped_fields(data),
        "hint": TRUNCATION_HINT,
    }
    # Escaping can inflate the preview; shrink until the whole record fits.
    while len(json.dumps(envelope, ensure_ascii=False)) > MAX_RESULT_CHARS and len(preview) > 200:
        preview = preview[: int(len(preview) * 0.8)]
        envelope["preview"] = preview
    return {
        "ok": bool(result.get("ok")),
        "data": envelope,
        "error": result.get("error"),
        "code": result.get("code"),
        "meta": result.get("meta") or {},
    }


class EvidenceLedger:
    """Every tool result gets an id the model can cite; ids are not writable by the model."""

    def __init__(self) -> None:
        self._items: dict[str, dict] = {}
        self._seq = 0

    # ------------------------------------------------------------------ write

    def add(self, tool: str, args: dict, result: dict) -> str:
        """Return "obs-1", "obs-2", ...; shape the result if it exceeds 1500 chars.

        Repeating the same (tool, args) is deliberately *not* merged: the model
        may retry with the same arguments after a transient failure, and the
        second observation still deserves its own id.
        """
        self._seq += 1
        obs_id = f"obs-{self._seq}"
        payload = result if isinstance(result, dict) else {"ok": False, "data": result}
        text = json.dumps(payload, ensure_ascii=False, default=str)
        effective = payload if len(text) <= MAX_RESULT_CHARS else _truncate(payload)
        self._items[obs_id] = {
            "obs_id": obs_id,
            "tool": str(tool),
            "args_sha1": digest_arguments(args),
            "result": effective,
            # Full payload stays in memory for this run only. ``to_dict`` never
            # writes it, so a checkpoint cannot leak unshaped merchant data.
            "full_result": payload,
            "truncated": effective is not payload,
            "result_sha1": _sha1_text(text),
            "summary": _summarise(tool, effective),
            "numeric_facts": _numeric_facts(payload),
        }
        return obs_id

    # ------------------------------------------------------------------- read

    def get(self, obs_id: str) -> dict | None:
        item = self._items.get(str(obs_id))
        return copy.deepcopy(item) if item is not None else None

    def result(self, obs_id: str) -> dict | None:
        """The model-facing result after shaping (what the gateway should return)."""
        item = self._items.get(str(obs_id))
        if item is None:
            return None
        return copy.deepcopy(item.get("result"))

    def exists(self, obs_id: str) -> bool:
        return str(obs_id) in self._items

    def tool_of(self, obs_id: str) -> str:
        item = self._items.get(str(obs_id))
        return str(item.get("tool") or "") if item else ""

    def summary_of(self, obs_id: str) -> str:
        item = self._items.get(str(obs_id))
        return str(item.get("summary") or "") if item else ""

    def digest(self) -> str:
        """Format like "3 observations: get_metrics x2, get_reviews x1" for prompt headers."""
        counts: "OrderedDict[str, int]" = OrderedDict()
        for item in self._items.values():
            tool = str(item.get("tool") or "unknown")
            counts[tool] = counts.get(tool, 0) + 1
        total = len(self._items)
        if not total:
            return "0 observations"
        label = "observation" if total == 1 else "observations"
        parts = ", ".join(f"{tool} x{count}" for tool, count in counts.items())
        return f"{total} {label}: {parts}"

    # ------------------------------------------------------------ persistence

    def to_dict(self) -> dict:
        """Checkpoint form: no payloads, only ids, digests, summary and numbers."""
        items = {}
        for obs_id, item in self._items.items():
            items[obs_id] = {
                "obs_id": obs_id,
                "tool": item.get("tool", ""),
                "args_sha1": item.get("args_sha1", ""),
                "result_sha1": item.get("result_sha1", ""),
                "summary": str(item.get("summary") or "")[:MAX_SUMMARY_CHARS],
                "numeric_facts": list(item.get("numeric_facts") or []),
            }
        return {"seq": self._seq, "items": items}

    @classmethod
    def from_dict(cls, payload: dict) -> "EvidenceLedger":
        ledger = cls()
        document = payload if isinstance(payload, dict) else {}
        raw_items = document.get("items")
        if isinstance(raw_items, dict):
            for key, value in raw_items.items():
                if not isinstance(value, dict):
                    continue
                # A restored item has an id and a summary but no payload: that
                # is enough to validate a citation, not enough to re-feed it.
                ledger._items[str(key)] = {
                    "obs_id": str(value.get("obs_id") or key),
                    "tool": str(value.get("tool") or ""),
                    "args_sha1": str(value.get("args_sha1") or ""),
                    "result": None,
                    "full_result": None,
                    "truncated": False,
                    "result_sha1": str(value.get("result_sha1") or ""),
                    "summary": str(value.get("summary") or "")[:MAX_SUMMARY_CHARS],
                    "numeric_facts": list(value.get("numeric_facts") or []),
                }
        try:
            seq = int(document.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        # A hand-edited checkpoint must not hand out an id that already exists.
        ledger._seq = max(seq, len(ledger._items))
        return ledger

    # ------------------------------------------------------------------ dunder

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, obs_id: object) -> bool:
        return str(obs_id) in self._items


__all__ = [
    "EvidenceLedger",
    "MAX_PREVIEW_CHARS",
    "MAX_RESULT_CHARS",
    "MAX_SUMMARY_CHARS",
    "TRUNCATION_HINT",
    "digest_arguments",
]
