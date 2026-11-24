"""Persistent memory for the operations director.

Four files, one writer. The director is the only agent allowed to write here;
the workshop and compliance agents may read but never write, so a bad batch
cannot quietly rewrite the store's history.

    memory/store_profile.json      current picture of the store
    memory/experiments.jsonl      one line per experiment, append only
    memory/decisions.jsonl        why this week looked the way it did
    memory/lessons.json           what worked, what did not

Step 6 added a fifth file in the same directory — ``facts.jsonl`` — but it is a
different thing with a different owner, so it lives in ``memory/facts.py`` and
never touches the four files above.

Two properties matter more than features:

* **Atomicity.** Every write goes to a temp file and is moved into place, so a
  crash mid-write cannot leave a half-parsed profile behind.
* **Idempotency.** Re-running the same batch must not duplicate experiments.
  Records carry an id; duplicates are skipped, not appended twice.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import default_data_dir

PROFILE_VERSIONS = 8
_SECRET_HINTS = ("api_key", "apikey", "token", "secret", "password")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _scrub(payload: Any) -> Any:
    """Drop anything that looks like a credential before it reaches disk."""
    if isinstance(payload, dict):
        clean = {}
        for key, value in payload.items():
            if any(hint in str(key).lower() for hint in _SECRET_HINTS):
                continue
            clean[key] = _scrub(value)
        return clean
    if isinstance(payload, list):
        return [_scrub(item) for item in payload]
    return payload


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


class MemoryStore:
    """File-backed memory with atomic writes and duplicate protection."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else default_data_dir() / "memory"
        self.bad_lines = 0
        # Counting the same broken line twice on every read would make the
        # health panel drift upward for no reason, so failures are keyed.
        self._bad_keys: set[tuple[str, str]] = set()

    @property
    def profile_path(self) -> Path:
        return self.root / "store_profile.json"

    @property
    def experiments_path(self) -> Path:
        return self.root / "experiments.jsonl"

    @property
    def decisions_path(self) -> Path:
        return self.root / "decisions.jsonl"

    @property
    def lessons_path(self) -> Path:
        return self.root / "lessons.json"

    @property
    def history_dir(self) -> Path:
        return self.root / "profile_history"

    @property
    def snapshot_dir(self) -> Path:
        return self.root / "snapshots"

    # ---------------------------------------------------------------- reading

    def read_profile(self) -> dict:
        return self._read_json(self.profile_path)

    def read_experiments(self, limit: int = 20, include_open: bool = True) -> list[dict]:
        """Most recent N, always including experiments still under observation.

        Open experiments must never fall out of the window: they are the ones
        the director still has to judge.
        """
        items = self._read_jsonl(self.experiments_path)
        if not items:
            return []
        items.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
        recent = items[: max(0, limit)]
        if not include_open:
            return recent
        seen = {id(item) for item in recent}
        open_items = [
            item
            for item in items
            if str(item.get("status")) == "observing" and id(item) not in seen
        ]
        return recent + open_items

    def read_decisions(self, limit: int = 10) -> list[dict]:
        items = self._read_jsonl(self.decisions_path)
        items.sort(key=lambda item: str(item.get("at") or ""), reverse=True)
        return items[: max(0, limit)]

    def read_lessons(self) -> list[dict]:
        payload = self._read_json(self.lessons_path)
        items = payload.get("lessons") if isinstance(payload, dict) else payload
        return items if isinstance(items, list) else []

    def context(self, run_id: str = "", max_experiments: int = 20, max_decisions: int = 10) -> dict:
        """Everything the director should see before deciding anything."""
        return {
            "run_id": run_id,
            "store_profile": self.read_profile(),
            "experiments": self.read_experiments(limit=max_experiments, include_open=True),
            "decisions": self.read_decisions(limit=max_decisions),
            "lessons": self.read_lessons(),
            "saved_at": _now(),
        }

    def health(self) -> dict:
        """Cheap integrity report for the console's memory panel."""
        return {
            "root": str(self.root),
            "profile": bool(self.read_profile()),
            "experiments": len(self._read_jsonl(self.experiments_path)),
            "decisions": len(self._read_jsonl(self.decisions_path)),
            "lessons": len(self.read_lessons()),
            "bad_lines": self.bad_lines,
        }

    # ---------------------------------------------------------------- writing

    def write_profile(self, payload: dict, run_id: str = "") -> Path:
        """Replace the profile, keeping a bounded history for replay."""
        document = _scrub(dict(payload or {}))
        document["updated_at"] = _now()
        if run_id:
            document["updated_by"] = run_id
        _atomic_write_json(self.profile_path, document)
        self._keep_profile_version(document)
        return self.profile_path

    def append_experiment(self, payload: dict) -> bool:
        """Append unless this experiment is already recorded."""
        record = _scrub(dict(payload or {}))
        experiment_id = str(record.get("experiment_id") or "").strip()
        if not experiment_id:
            raise ValueError("experiment_id is required")
        existing = {
            str(item.get("experiment_id")) for item in self._read_jsonl(self.experiments_path)
        }
        if experiment_id in existing:
            return False
        record.setdefault("created_at", _now())
        self._append_jsonl(self.experiments_path, record)
        return True

    def append_decision(self, payload: dict) -> bool:
        record = _scrub(dict(payload or {}))
        decision_id = str(record.get("decision_id") or "").strip()
        if not decision_id:
            raise ValueError("decision_id is required")
        existing = {str(item.get("decision_id")) for item in self._read_jsonl(self.decisions_path)}
        if decision_id in existing:
            return False
        record.setdefault("at", _now())
        self._append_jsonl(self.decisions_path, record)
        return True

    def write_lessons(self, items: list[dict], limit: int = 200) -> Path:
        cleaned = [_scrub(item) for item in (items or []) if isinstance(item, dict)]
        cleaned = cleaned[: max(1, limit)]
        _atomic_write_json(self.lessons_path, {"updated_at": _now(), "lessons": cleaned})
        return self.lessons_path

    def update_experiment(self, experiment_id: str, patch: dict) -> bool:
        """Rewrite one line in place. Used when an experiment reaches a verdict."""
        items = self._read_jsonl(self.experiments_path)
        changed = False
        for index, item in enumerate(items):
            if str(item.get("experiment_id")) != experiment_id:
                continue
            merged = dict(item)
            merged.update(_scrub(dict(patch or {})))
            merged["updated_at"] = _now()
            items[index] = merged
            changed = True
            break
        if not changed:
            return False
        text = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items)
        _atomic_write_text(self.experiments_path, text)
        return True

    def snapshot(self, run_id: str) -> Path:
        """Freeze what the director saw, so a decision can be audited later."""
        if not run_id:
            raise ValueError("run_id is required")
        document = self.context(run_id=run_id)
        path = self.snapshot_dir / f"{run_id}.json"
        _atomic_write_json(path, document)
        return path

    # ---------------------------------------------------------------- helpers

    def _keep_profile_version(self, document: dict) -> None:
        self.history_dir.mkdir(parents=True, exist_ok=True)
        # Microseconds matter: two writes in the same second must not collide.
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        _atomic_write_json(self.history_dir / f"{stamp}.json", document)
        versions = sorted(self.history_dir.glob("*.json"))
        for stale in versions[:-PROFILE_VERSIONS]:
            try:
                stale.unlink()
            except OSError:
                pass

    def _note_bad(self, path: Path, tag: str) -> None:
        key = (str(path), tag)
        if key not in self._bad_keys:
            self._bad_keys.add(key)
            self.bad_lines += 1

    def _read_json(self, path: Path) -> dict:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self._note_bad(path, "whole")
            return {}
        return payload if isinstance(payload, dict) else {}

    def _read_jsonl(self, path: Path) -> list[dict]:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return []
        items: list[dict] = []
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
                items.append(payload)
        return items

    def _append_jsonl(self, path: Path, record: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        prefix = ""
        try:
            existing = path.read_text(encoding="utf-8")
            if existing and not existing.endswith("\n"):
                prefix = "\n"
        except OSError:
            existing = ""
        with path.open("a", encoding="utf-8") as handle:
            handle.write(prefix + json.dumps(record, ensure_ascii=False) + "\n")
