"""Run state: everything needed to read back what a run did.

The state is a checkpoint, not a resume point. Restoring it tells you where the
run stopped and what it had seen; continuing a half-finished conversation is a
different problem and deliberately not solved here.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RunState:
    run_id: str
    agent: str = ""
    step: int = 0
    messages: list[dict] = field(default_factory=list)
    tool_calls: int = 0
    tokens: int = 0
    status: str = "running"
    outcome: dict | None = None
    ledger: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "agent": self.agent,
            "step": self.step,
            "messages": list(self.messages),
            "tool_calls": self.tool_calls,
            "tokens": self.tokens,
            "status": self.status,
            "outcome": self.outcome,
            "ledger": self.ledger,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "RunState":
        """Tolerant reader: a hand-edited or older checkpoint must not crash a run."""
        document = payload if isinstance(payload, dict) else {}
        messages = document.get("messages")
        outcome = document.get("outcome")
        ledger = document.get("ledger")
        return cls(
            run_id=str(document.get("run_id") or ""),
            agent=str(document.get("agent") or ""),
            step=_int(document.get("step")),
            messages=[item for item in messages if isinstance(item, dict)]
            if isinstance(messages, list)
            else [],
            tool_calls=_int(document.get("tool_calls")),
            tokens=_int(document.get("tokens")),
            status=str(document.get("status") or "running"),
            outcome=outcome if isinstance(outcome, dict) else None,
            ledger=ledger if isinstance(ledger, dict) else {},
        )


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = ["RunState"]
