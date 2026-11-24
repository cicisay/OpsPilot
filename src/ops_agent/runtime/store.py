"""Event log and state snapshots for one run.

Two files per run, both under ``<data root>/runs``:

    <run_id>.jsonl        append-only event stream
    <run_id>.state.json   last known state, replaced atomically

JSONL is append-only on purpose: a crash mid-run leaves the events that did
happen, and a single bad line must not hide the other 200. State is replaced
atomically so a reader never sees half a JSON object.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..config import default_data_dir
from .state import RunState


class EventStore:
    def __init__(self, root: Path | str | None = None) -> None:
        # ``root`` may be the data directory or the runs directory itself. The
        # default is already ``.../runs``; passing a temp dir in tests should
        # not create ``tmp/runs/runs``.
        base = Path(root) if root is not None else default_data_dir() / "runs"
        self.root = base if base.name == "runs" else base / "runs"
        self.bad_lines = 0
        # The same broken line must not be counted again on every read.
        self._bad_keys: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------ paths

    def events_path(self, run_id: str) -> Path:
        return self.root / f"{_safe(run_id)}.jsonl"

    def state_path(self, run_id: str) -> Path:
        return self.root / f"{_safe(run_id)}.state.json"

    # ----------------------------------------------------------------- events

    def append(self, run_id: str, event: dict) -> None:
        path = self.events_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        prefix = ""
        try:
            existing = path.read_text(encoding="utf-8")
            if existing and not existing.endswith("\n"):
                prefix = "\n"
        except OSError:
            pass
        with path.open("a", encoding="utf-8") as handle:
            handle.write(prefix + json.dumps(event, ensure_ascii=False) + "\n")

    def read(self, run_id: str) -> list[dict]:
        path = self.events_path(run_id)
        try:
            raw = path.read_text(encoding="utf-8")
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
                self._note_bad(path, str(number))
                continue
            if isinstance(payload, dict):
                events.append(payload)
            else:
                self._note_bad(path, str(number))
        return events

    # ------------------------------------------------------------------ state

    def save_state(self, state: RunState) -> None:
        """Atomic replace. Failures propagate: the caller records ``store_error``."""
        path = self.state_path(state.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state.to_dict(), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def load_state(self, run_id: str) -> RunState | None:
        path = self.state_path(run_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self._note_bad(path, "whole")
            return None
        if not isinstance(payload, dict):
            self._note_bad(path, "whole")
            return None
        return RunState.from_dict(payload)

    # ---------------------------------------------------------------- helpers

    def _note_bad(self, path: Path, tag: str) -> None:
        key = (str(path), tag)
        if key not in self._bad_keys:
            self._bad_keys.add(key)
            self.bad_lines += 1


def _safe(run_id: str) -> str:
    """A run id is a filename: strip separators instead of escaping the folder."""
    cleaned = str(run_id or "").strip().replace("/", "_").replace("\\", "_")
    return cleaned or "unnamed"


__all__ = ["EventStore"]
