import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.llm import LLMReply, ToolCall  # noqa: E402
from ops_agent.runtime import Budget, EvidenceLedger, EventStore, Runtime  # noqa: E402
from ops_agent.runtime.loop import BUDGET_NUDGE, NO_TOOL_NUDGE  # noqa: E402


def reply(text: str = "", calls: list | None = None, tokens: int = 10) -> LLMReply:
    tool_calls = [
        ToolCall(id=call_id, name=name, arguments=arguments, raw_arguments=json.dumps(arguments))
        for call_id, name, arguments in (calls or [])
    ]
    return LLMReply(
        text=text,
        prompt_tokens=tokens,
        completion_tokens=0,
        tool_calls=tool_calls,
        finish_reason="tool_calls" if tool_calls else "stop",
    )


class ScriptedClient:
    """Returns a fixed list of replies and records what the loop showed it."""

    def __init__(self, replies: list[LLMReply]) -> None:
        self.replies = list(replies)
        self.seen: list[list[dict]] = []
        self.kwargs: list[dict] = []

    def chat(self, messages, **kwargs):
        if not self.replies:
            raise AssertionError("scripted client ran out of replies")
        self.seen.append(copy.deepcopy(messages))
        self.kwargs.append(dict(kwargs))
        return self.replies.pop(0)


class FakeGateway:
    """Records calls, writes to the real ledger, and accepts the budget injection."""

    def __init__(self, ledger: EvidenceLedger) -> None:
        self.ledger = ledger
        self.policy = SimpleNamespace(role="tester")
        self.max_tool_calls: int | None = None
        self.calls: list[tuple[str, dict]] = []
        self.budget_injected: int | None = None

    def set_budget(self, value: int | None) -> None:
        self.max_tool_calls = value
        self.budget_injected = value

    def specs(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_metrics",
                    "description": "",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    def call(self, name: str, arguments: dict, ctx) -> dict:
        self.calls.append((name, arguments))
        data = {"rows": len(self.calls)}
        payload = {"ok": True, "data": data, "error": None, "code": None, "meta": {}}
        obs = self.ledger.add(name, arguments, payload)
        return {"obs": obs, "ok": True, "code": None, "data": data, "error": None}


class LoopTests(unittest.TestCase):
    def make(self, replies, budget=None):
        self.ledger = EvidenceLedger()
        self.gateway = FakeGateway(self.ledger)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = EventStore(Path(self.tmp.name))
        self.client = ScriptedClient(replies)
        self.runtime = Runtime(
            self.client,
            self.gateway,
            self.ledger,
            self.store,
            budget=budget or Budget(),
        )
        return self.runtime

    def test_two_tool_calls_then_done(self):
        terminal = {"submit": lambda args: (True, {"status": "done", "answer": args.get("answer")}, [])}
        runtime = self.make(
            [
                reply(calls=[("c1", "get_metrics", {"sku_id": "s1"})]),
                reply(calls=[("c2", "get_metrics", {"sku_id": "s2"})]),
                reply(calls=[("c3", "submit", {"answer": "ok"})]),
            ]
        )
        result = runtime.run(run_id="r1", system_prompt="sys", seed={"task": 1}, terminal=terminal)
        self.assertEqual(result.status, "done")
        self.assertEqual(result.steps, 3)
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(result.tokens, 30)
        self.assertEqual(result.payload["answer"], "ok")
        # The assistant turn and its tool replies are paired by id.
        observed = self.client.seen[1]
        self.assertEqual(observed[2]["role"], "assistant")
        self.assertEqual(observed[2]["tool_calls"][0]["id"], "c1")
        self.assertEqual(observed[3]["role"], "tool")
        self.assertEqual(observed[3]["tool_call_id"], "c1")
        self.assertEqual(self.gateway.budget_injected, Budget().max_tool_calls)
        # Tools are offered to the model, and json_mode is off on that path.
        self.assertTrue(self.client.kwargs[0]["tools"])
        self.assertNotIn("json_mode", self.client.kwargs[0])

    def test_terminal_errors_are_fed_back_then_succeed(self):
        attempts = {"n": 0}

        def submit(args):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return False, None, ["missing field: price"]
            return True, {"status": "done"}, []

        runtime = self.make(
            [
                reply(calls=[("c1", "submit", {})]),
                reply(calls=[("c2", "submit", {"price": 1899})]),
            ]
        )
        result = runtime.run(run_id="r2", system_prompt="sys", seed={}, terminal={"submit": submit})
        self.assertEqual(result.status, "done")
        self.assertEqual(attempts["n"], 2)
        self.assertIn("missing field: price", json.dumps(self.client.seen[1], ensure_ascii=False))

    def test_token_budget_wraps_up_then_exhausts(self):
        runtime = self.make(
            [
                reply(calls=[("c1", "get_metrics", {"sku_id": "s1"})], tokens=80),
                reply(calls=[("c2", "get_metrics", {"sku_id": "s2"})], tokens=80),
                reply(text="nothing to add", tokens=20),
            ],
            budget=Budget(max_total_tokens=100, max_steps=10),
        )
        result = runtime.run(run_id="r3", system_prompt="sys", seed={}, terminal={"submit": lambda a: (True, {}, [])})
        self.assertEqual(result.status, "budget_exhausted")
        self.assertIsNone(result.payload)
        # The nudge is injected after the over-budget turn, and the model gets
        # exactly one more step before the loop stops.
        self.assertEqual(self.client.seen[2][-1], {"role": "user", "content": BUDGET_NUDGE})
        self.assertEqual(result.steps, 3)

    def test_three_identical_calls_emit_degraded_event(self):
        same = ("c", "get_metrics", {"sku_id": "s1"})
        runtime = self.make(
            [
                reply(calls=[("c1", same[1], same[2])]),
                reply(calls=[("c2", same[1], same[2])]),
                reply(calls=[("c3", same[1], same[2])]),
                reply(calls=[("c4", "submit", {})]),
            ]
        )
        result = runtime.run(run_id="r4", system_prompt="sys", seed={}, terminal={"submit": lambda a: (True, {"status": "done"}, [])})
        self.assertEqual(result.status, "done")
        kinds = [event.get("kind") for event in self.store.read("r4")]
        self.assertIn("degraded", kinds)

    def test_declined_terminal(self):
        terminal = {"decline": lambda args: (True, {"status": "declined", "reason": "no data"}, [])}
        runtime = self.make([reply(calls=[("c1", "decline", {})])])
        result = runtime.run(run_id="r5", system_prompt="sys", seed={}, terminal=terminal)
        self.assertEqual(result.status, "declined")
        self.assertEqual(result.payload["reason"], "no data")

    def test_text_only_twice_then_no_tool_call(self):
        runtime = self.make(
            [reply(text="thinking"), reply(text="still thinking"), reply(text="done thinking")],
            budget=Budget(max_steps=10),
        )
        result = runtime.run(run_id="r6", system_prompt="sys", seed={}, terminal={"submit": lambda a: (True, {}, [])})
        self.assertEqual(result.status, "no_tool_call")
        self.assertEqual(result.tool_calls, 0)
        nudges = [item for item in self.client.seen[-1] if item.get("content") == NO_TOOL_NUDGE]
        self.assertEqual(len(nudges), 2)

    def test_compaction_keeps_obs_ids_citable(self):
        replies = [
            reply(calls=[(f"c{index}", "get_metrics", {"sku_id": f"s{index}"})]) for index in range(9)
        ]
        replies.append(reply(calls=[("ct", "submit", {})]))
        runtime = self.make(replies, budget=Budget(max_steps=14))
        result = runtime.run(run_id="r7", system_prompt="sys", seed={}, terminal={"submit": lambda a: (True, {"status": "done"}, [])})
        self.assertEqual(result.status, "done")
        final_view = self.client.seen[-1]
        compacted = [item for item in final_view if str(item.get("content", "")).startswith("[compacted]")]
        self.assertEqual(len(compacted), 3)
        self.assertIn("obs-1", compacted[0]["content"])
        self.assertIn("[compacted]", json.dumps(final_view, ensure_ascii=False))

    def test_state_round_trip_matches_result(self):
        runtime = self.make(
            [
                reply(calls=[("c1", "get_metrics", {"sku_id": "s1"})]),
                reply(calls=[("c2", "submit", {})]),
            ]
        )
        result = runtime.run(run_id="r8", system_prompt="sys", seed={}, terminal={"submit": lambda a: (True, {"status": "done"}, [])})
        state = self.store.load_state("r8")
        self.assertIsNotNone(state)
        self.assertEqual(state.step, result.steps)
        self.assertEqual(state.tokens, result.tokens)
        self.assertEqual(state.status, result.status)
        self.assertEqual(state.tool_calls, result.tool_calls)
        self.assertEqual(state.ledger["seq"], 1)


if __name__ == "__main__":
    unittest.main()
