"""The bounded loop.

One turn is: call the model, run the tools it asked for, feed the results back.
What makes it safe is everything around that sentence — a step cap, a token
cap, a wall-clock cap, a tool-call cap, a submit-attempt cap, and three
recovery nudges so a confused model gets a chance to correct itself before the
run is stopped.

The loop never decides business content. It only decides whether the model may
continue, and records why it stopped.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .state import RunState

# Prompts the model sees when it is stuck. Short on purpose: a long lecture
# about budgeting costs tokens and rarely changes the next action.
BUDGET_NUDGE = "Budget nearly exhausted. Finish now with the terminal tool using what you have."
NO_TOOL_NUDGE = "Use a tool, then finish with the terminal tool."
NO_PROGRESS_NUDGE = "No new information. Decide with what you have."
POLICY_NUDGE = "该工具不可用，只用已暴露的工具。"

# Two identical calls in a row earn a nudge; three earn a degraded event.
REPEAT_NUDGE_AT = 1
REPEAT_DEGRADE_AT = 2
MAX_NO_TOOL_REMINDERS = 2
# Reaching for a tool you were not given is a security signal: two strikes.
# Inventing a tool name that never existed is a competence signal, not an
# attack, so it gets four before the run is written off. Keeping one counter
# for both made a confused model look like a probing one.
POLICY_LIMIT = 2
MISUSE_LIMIT = 4
# The first rejection already deserves a reminder: with POLICY_LIMIT at two
# there is no "second time" to learn from.
POLICY_REMINDER_AT = 1
COMPACT_AFTER_TOOL_MESSAGES = 8
COMPACT_BATCH = 3

TERMINAL_STATUSES = frozenset({"done", "declined"})
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class Budget:
    max_steps: int = 14
    max_tool_calls: int = 16
    max_total_tokens: int = 60_000
    max_wall_seconds: float = 240.0
    max_submit_attempts: int = 3


@dataclass
class RunResult:
    status: str  # done | declined | budget_exhausted | step_limit | no_tool_call
    #              | invalid_task_orders | policy_violation | tool_misuse
    payload: dict | None
    steps: int
    tool_calls: int
    tokens: int


@dataclass
class _TerminalOutcome:
    final: bool
    payload: dict | None
    errors: list[str] = field(default_factory=list)


class Runtime:
    def __init__(
        self,
        client,
        gateway,
        ledger,
        store,
        budget: Budget | None = None,
        now=None,
        ctx: Any = None,
        trace=None,
        max_tokens: int | None = None,
    ) -> None:
        self.client = client
        self.gateway = gateway
        self.ledger = ledger
        self.store = store
        self.budget = budget or Budget()
        self._now = now or time.monotonic
        # A multi-SKU submit is a big JSON object; the default 4096 is enough
        # for prose, not always for five task orders.
        self.max_tokens = max_tokens
        # Tool context (platform, approvals, compliance). Injected, never built
        # here, so the runtime stays independent of the business layer.
        self.ctx = ctx
        # Optional TraceRecorder. One entry per model turn, so the run that the
        # console shows and the trace on disk describe the same steps.
        self.trace = trace
        # One source of truth for the tool budget: push it into the gateway so
        # a second copy cannot drift away from this one.
        setter = getattr(gateway, "set_budget", None)
        if callable(setter):
            setter(self.budget.max_tool_calls)

    # -------------------------------------------------------------------- run

    def run(
        self,
        *,
        run_id: str,
        system_prompt: str,
        seed: dict,
        terminal: dict[str, Callable],
    ) -> RunResult:
        handlers, terminal_specs = _prepare_terminal(terminal)
        # A Budget belongs to a run, not to a gateway instance: a gateway reused
        # for a second run must start from zero attempts.
        reset = getattr(self.gateway, "reset_counters", None)
        if callable(reset):
            reset()
        tools = self.gateway.specs() + terminal_specs
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            # Compact separators: the seed is the largest single message a run
            # sends, and the spaces after commas and colons are pure tokens.
            {
                "role": "user",
                "content": json.dumps(seed, ensure_ascii=False, default=str, separators=(",", ":")),
            },
        ]
        role = str(getattr(getattr(self.gateway, "policy", None), "role", "") or "")

        step = 0
        tool_calls = 0
        tokens = 0
        submit_attempts = 0
        no_tool_reminders = 0
        budget_warned = False
        permitted_rejections = 0
        unknown_rejections = 0
        rejected_counts: dict[str, int] = {}
        last_signature: tuple[str, str] | None = None
        repeats = 0
        started = self._now()

        state = RunState(run_id=run_id, agent=role, ledger=self.ledger.to_dict())

        def snapshot() -> None:
            state.step = step
            state.tokens = tokens
            state.tool_calls = tool_calls
            state.messages = list(messages)
            state.ledger = self.ledger.to_dict()
            self._save(state)

        def finish(status: str, payload: dict | None) -> RunResult:
            state.status = status
            state.outcome = payload
            snapshot()
            return RunResult(
                status=status,
                payload=payload,
                steps=step,
                tool_calls=tool_calls,
                tokens=tokens,
            )

        # Trace plumbing. ``executed`` is mutated in place so the closure sees
        # the current turn's calls without rebinding anything.
        executed: list[dict] = []
        last_reply = None

        def record_trace(note: str = "") -> None:
            if self.trace is None or last_reply is None:
                return
            prompt_tokens = int(getattr(last_reply, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(last_reply, "completion_tokens", 0) or 0)
            meta: dict = {}
            if note:
                meta["note"] = note
            # Retries are billed but invisible in the token count, so the trace
            # carries the request count next to the tokens it produced.
            attempts = int(getattr(last_reply, "attempts", 1) or 1)
            if attempts > 1:
                meta["attempts"] = attempts
            self.trace.record(
                agent=role or "runtime",
                model=str(getattr(last_reply, "model", "") or ""),
                messages=messages,
                response=str(getattr(last_reply, "text", "") or ""),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=int(getattr(last_reply, "latency_ms", 0) or 0),
                meta=meta or None,
                step=step,
                tool=",".join(item["tool"] for item in executed),
                args_hash=executed[-1]["args_hash"] if executed else "",
                obs_id=",".join(item["obs"] for item in executed if item.get("obs")),
                # This call's tokens, not the running total: one line is one
                # model call, and per-call is what the budget is measured on.
                tokens=prompt_tokens + completion_tokens,
            )

        def stop(status: str, payload: dict | None) -> RunResult:
            record_trace(status)
            return finish(status, payload)

        snapshot()
        while step < self.budget.max_steps:
            reply = self.client.chat(
                messages,
                tools=tools or None,
                tool_choice="auto",
                temperature=0.2,
                max_tokens=self.max_tokens,
            )
            last_reply = reply
            executed[:] = []
            step += 1
            tokens += reply.total_tokens
            exceeded = self._exceeded(tokens, tool_calls, started)

            if not reply.tool_calls:
                messages.append({"role": "assistant", "content": reply.text})
                if not exceeded and no_tool_reminders < MAX_NO_TOOL_REMINDERS:
                    messages.append({"role": "user", "content": NO_TOOL_NUDGE})
                    no_tool_reminders += 1
                    record_trace("no_tool")
                    snapshot()
                    continue
                if not exceeded:
                    return stop("no_tool_call", None)
            else:
                messages.append(_assistant_message(reply))
                pending: list[str] = []
                for call in reply.tool_calls:
                    name = call.name
                    arguments = call.arguments if isinstance(call.arguments, dict) else {}

                    if name in handlers:
                        executed.append(
                            {"tool": name, "obs": "", "args_hash": _signature(name, arguments)[1]}
                        )
                        outcome = _invoke(handlers[name], arguments)
                        if outcome.errors:
                            submit_attempts += 1
                            messages.append(
                                _tool_message(call.id, {"ok": False, "errors": outcome.errors})
                            )
                            self._event(
                                run_id,
                                {"kind": "submit_rejected", "step": step, "attempt": submit_attempts},
                            )
                            if submit_attempts >= self.budget.max_submit_attempts:
                                return stop("invalid_task_orders", None)
                            continue
                        if outcome.final:
                            return stop(_terminal_status(outcome.payload), outcome.payload)
                        messages.append(_tool_message(call.id, {"ok": True, "data": outcome.payload}))
                        continue

                    tool_calls += 1
                    result = self.gateway.call(name, arguments, self.ctx)
                    messages.append(_tool_message(call.id, result))
                    executed.append(
                        {
                            "tool": name,
                            "obs": str(result.get("obs") or ""),
                            "args_hash": _signature(name, arguments)[1],
                        }
                    )

                    code = str(result.get("code") or "")
                    if code in ("unknown_tool", "tool_not_permitted"):
                        rejected_counts[name] = rejected_counts.get(name, 0) + 1
                        if rejected_counts[name] == POLICY_REMINDER_AT:
                            pending.append(POLICY_NUDGE)
                        permitted = code == "tool_not_permitted"
                        if permitted:
                            permitted_rejections += 1
                        else:
                            unknown_rejections += 1
                        self._event(
                            run_id,
                            {
                                "kind": "policy_rejected",
                                "step": step,
                                "tool": name,
                                "code": code,
                                "permitted": permitted_rejections,
                                "unknown": unknown_rejections,
                            },
                        )
                        # Probing is not a conversation: stop before the model
                        # can spend the rest of the turn on more guesses.
                        if permitted_rejections >= POLICY_LIMIT:
                            return stop("policy_violation", None)
                        if unknown_rejections >= MISUSE_LIMIT:
                            return stop("tool_misuse", None)
                        continue

                    signature = _signature(name, arguments)
                    if signature == last_signature:
                        repeats += 1
                    else:
                        last_signature, repeats = signature, 0
                    if repeats == REPEAT_NUDGE_AT:
                        pending.append(NO_PROGRESS_NUDGE)
                    elif repeats >= REPEAT_DEGRADE_AT:
                        self._event(
                            run_id,
                            {"kind": "degraded", "step": step, "tool": name, "repeats": repeats},
                        )

                for text in pending:
                    messages.append({"role": "user", "content": text})
                self._compact(messages)

            if exceeded:
                if not budget_warned:
                    messages.append({"role": "user", "content": BUDGET_NUDGE})
                    budget_warned = True
                    record_trace("budget_nudge")
                    snapshot()
                    continue
                return stop("budget_exhausted", None)
            record_trace()
            snapshot()

        return stop("step_limit", None)

    # --------------------------------------------------------------- budgets

    def _exceeded(self, tokens: int, tool_calls: int, started: float) -> bool:
        if tokens > self.budget.max_total_tokens:
            return True
        if tool_calls > self.budget.max_tool_calls:
            return True
        if self.budget.max_wall_seconds and (self._now() - started) > self.budget.max_wall_seconds:
            return True
        return False

    # ------------------------------------------------------------ compaction

    def _compact(self, messages: list[dict]) -> None:
        """Replace the oldest raw tool payloads with one citable line each.

        The id stays in the text: a model that cited obs-1 three turns ago must
        still be able to cite it after its payload is gone.
        """
        indexes = [index for index, item in enumerate(messages) if item.get("role") == "tool"]
        if len(indexes) <= COMPACT_AFTER_TOOL_MESSAGES:
            return
        compactable = [
            index
            for index in indexes
            if not str(messages[index].get("content") or "").startswith("[compacted]")
        ]
        for index in compactable[:COMPACT_BATCH]:
            messages[index]["content"] = self._compact_line(str(messages[index].get("content") or ""))

    def _compact_line(self, text: str) -> str:
        try:
            payload = json.loads(text)
        except ValueError:
            return "[compacted] " + text[:120]
        if not isinstance(payload, dict):
            return "[compacted] " + text[:120]
        obs_id = str(payload.get("obs") or "")
        tool = self.ledger.tool_of(obs_id) if obs_id else ""
        numbers: list[str] = []
        for token in _NUMBER.findall(text):
            if token not in numbers:
                numbers.append(token)
            if len(numbers) >= 5:
                break
        label = f"{obs_id}: {tool}".strip(": ") or "observation"
        return f"[compacted] {label} ok={payload.get('ok')} numbers={','.join(numbers) or '-'}"

    # ------------------------------------------------------------ persistence

    def _save(self, state: RunState) -> None:
        try:
            self.store.append(
                state.run_id,
                {
                    "kind": "step",
                    "n": state.step,
                    "tokens": state.tokens,
                    "tool_calls": state.tool_calls,
                    "status": state.status,
                },
            )
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not stop a run
            self._store_error(state.run_id, exc)
        try:
            self.store.save_state(state)
        except Exception as exc:  # noqa: BLE001
            self._store_error(state.run_id, exc)

    def _event(self, run_id: str, event: dict) -> None:
        try:
            self.store.append(run_id, event)
        except Exception as exc:  # noqa: BLE001
            self._store_error(run_id, exc)

    def _store_error(self, run_id: str, exc: Exception) -> None:
        try:
            self.store.append(run_id, {"kind": "store_error", "error": str(exc)})
        except Exception:  # noqa: BLE001 - the disk is already broken
            pass


# --------------------------------------------------------------------- helpers


def _signature(name: str, arguments: dict) -> tuple[str, str]:
    payload = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False, default=str)
    return name, hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _assistant_message(reply) -> dict:
    calls = []
    for call in reply.tool_calls:
        raw = call.raw_arguments or json.dumps(call.arguments, ensure_ascii=False)
        calls.append(
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": raw},
            }
        )
    return {"role": "assistant", "content": reply.text or "", "tool_calls": calls}


def _tool_message(call_id: str, payload: dict) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")),
    }


def _prepare_terminal(terminal: dict) -> tuple[dict, list[dict]]:
    """Split {name: handler} (or {name: (spec, handler)}) into handlers and schemas.

    The schema matters: a terminal tool without one is a name the model can see
    but cannot fill in. A callable may carry a richer spec from the business
    layer; otherwise a permissive object is used.
    """
    handlers: dict[str, Callable] = {}
    specs: list[dict] = []
    for key, entry in (terminal or {}).items():
        name = str(key)
        spec: dict | None = None
        handler = entry
        if isinstance(entry, tuple) and len(entry) == 2:
            maybe_spec, handler = entry
            if isinstance(maybe_spec, dict):
                spec = maybe_spec
        handlers[name] = handler

        if spec and "function" in spec:
            specs.append(spec)
            continue
        description = ""
        if callable(handler) and handler.__doc__:
            description = handler.__doc__.strip().splitlines()[0]
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description or f"Terminal tool {name}: call it to finish the run.",
                    "parameters": spec
                    or {"type": "object", "properties": {}, "additionalProperties": True},
                },
            }
        )
    return handlers, specs


def _invoke(handler: Callable, arguments: dict) -> _TerminalOutcome:
    try:
        raw = handler(arguments)
    except Exception as exc:  # noqa: BLE001 - a bad submit is data, not a crash
        return _TerminalOutcome(False, None, [f"terminal tool failed: {exc}"])
    if not isinstance(raw, tuple) or len(raw) != 3:
        return _TerminalOutcome(
            False, None, ["terminal tool must return (final, payload, errors)"]
        )
    final, payload, errors = raw
    return _TerminalOutcome(
        bool(final),
        payload if isinstance(payload, dict) else None,
        [str(item) for item in (errors or [])],
    )


def _terminal_status(payload: dict | None) -> str:
    status = str((payload or {}).get("status") or "").strip()
    return status if status in TERMINAL_STATUSES else "done"


__all__ = ["Budget", "RunResult", "Runtime"]
