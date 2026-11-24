"""Append-only trace of every model call, so any run can be replayed or audited.

One jsonl file per run under ``data/llm_trace``. Prompts are clipped so a long
run cannot balloon into hundreds of megabytes.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import TRACE_DIR

_CLIP = 4000


def _clip(text: str, limit: int = _CLIP) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…[clipped {len(text) - limit}]"


class TraceRecorder:
    def __init__(self, run_id: str | None = None, path: Path | str | None = None) -> None:
        self.run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = Path(path) if path else TRACE_DIR / f"{self.run_id}.jsonl"
        self.entries: list[dict] = []
        self.total_tokens = 0
        self.failures = 0

    def record(
        self,
        *,
        agent: str,
        ok: bool = True,
        model: str = "",
        messages: list[dict] | None = None,
        response: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: int = 0,
        error: str | None = None,
        meta: dict[str, Any] | None = None,
        step: int | None = None,
        tool: str = "",
        args_hash: str = "",
        obs_id: str = "",
        tokens: int | None = None,
    ) -> None:
        entry = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "agent": agent,
            "ok": ok,
            "model": model,
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "latency_ms": int(latency_ms),
            "error": error,
            "meta": meta or {},
            "messages": [
                {"role": str(item.get("role")), "content": _clip(str(item.get("content", "")))}
                for item in (messages or [])
            ],
            "response": _clip(response or ""),
        }
        # Agent-loop fields. Only written when the caller has them, so the
        # one-shot director's lines keep their original shape.
        if step is not None:
            entry["step"] = int(step)
        if tool:
            entry["tool"] = tool
        if args_hash:
            entry["args_hash"] = args_hash
        if obs_id:
            entry["obs_id"] = obs_id
        entry["tokens"] = int(
            tokens if tokens is not None else entry["prompt_tokens"] + entry["completion_tokens"]
        )
        self.entries.append(entry)
        self.total_tokens += entry["prompt_tokens"] + entry["completion_tokens"]
        if not ok:
            self.failures += 1
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            # Tracing must never break a run.
            pass

    def summary(self) -> dict:
        return {
            "run_id": self.run_id,
            "calls": len(self.entries),
            "failures": self.failures,
            "tokens": self.total_tokens,
            "path": str(self.path),
        }
