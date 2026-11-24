#!/usr/bin/env python3
"""Print the tool sequence of every recorded model run.

Reads ``data/llm_trace/*.jsonl`` (one line per model call) and shows, per run,
which tools the model asked for and in what order. This is the cheapest way to
answer "did the agent actually look things up, or did it answer from the seed?".

    python scripts/inspect_trace.py
    python scripts/inspect_trace.py --last 5 --dir data/llm_trace
"""

from __future__ import annotations

import argparse
import json
from collections import OrderedDict
from pathlib import Path


def load_runs(directory: Path) -> "OrderedDict[str, list[dict]]":
    runs: "OrderedDict[str, list[dict]]" = OrderedDict()
    for path in sorted(directory.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            run_id = str(entry.get("run_id") or path.stem)
            runs.setdefault(run_id, []).append(entry)
    return runs


def tools_of(entries: list[dict]) -> list[str]:
    """Consecutive order, repeats kept: calling get_metrics twice is a signal."""
    sequence: list[str] = []
    for entry in entries:
        for name in str(entry.get("tool") or "").split(","):
            name = name.strip()
            if name:
                sequence.append(name)
    return sequence


def tokens_of(entry: dict) -> int:
    """``tokens`` is newer than the oldest traces; fall back to the two counters."""
    value = entry.get("tokens")
    if value is None:
        return int(entry.get("prompt_tokens") or 0) + int(entry.get("completion_tokens") or 0)
    return int(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="打印每次运行的模型调用与工具序列")
    parser.add_argument("--dir", default="data/llm_trace")
    parser.add_argument("--last", type=int, default=10)
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args()

    directory = Path(args.dir)
    if not directory.exists():
        print(f"没有 trace 目录: {directory}")
        return 1
    runs = load_runs(directory)
    if not runs:
        print(f"{directory} 里没有 .jsonl")
        return 1

    selected = list(runs.items())[-args.last :]
    report = []
    for run_id, entries in selected:
        per_call = [tokens_of(entry) for entry in entries]
        items = [
            {
                "step": entry.get("step"),
                "tool": entry.get("tool") or "",
                "obs": entry.get("obs_id") or "",
                "tokens": entry.get("tokens"),
                "note": (entry.get("meta") or {}).get("note", ""),
            }
            for entry in entries
        ]
        report.append(
            {
                "run_id": run_id,
                "calls": len(entries),
                # max matters for the per-call budget; total is what the bill sees.
                "max_tokens": max(per_call, default=0),
                "total_tokens": sum(per_call),
                "tools": tools_of(entries),
                "steps": items,
            }
        )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    for item in report:
        print(
            f"{item['run_id']}  calls={item['calls']}  "
            f"tokens/次 最大 {item['max_tokens']} · 合计 {item['total_tokens']}"
        )
        print("  tools: " + (" -> ".join(item["tools"]) if item["tools"] else "(none: 本次没有工具调用)"))
        for step in item["steps"]:
            if not step["tool"]:
                continue
            print(
                f"    step {step['step']}: {step['tool']}"
                + (f"  obs={step['obs']}" if step["obs"] else "")
                + (f"  ({step['note']})" if step["note"] else "")
            )
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
