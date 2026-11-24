import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.llm import LLMReply, ToolCall  # noqa: E402
from ops_agent.runtime import (  # noqa: E402
    AgentPolicy,
    Budget,
    EvidenceLedger,
    EventStore,
    Runtime,
    ToolGateway,
)
from ops_agent.runtime.loop import POLICY_NUDGE  # noqa: E402
from ops_agent.tools import ToolResult  # noqa: E402


def spec(name: str, side_effect: str = "read", description: str = "") -> dict:
    return {
        "name": name,
        "description": description or f"{name} 测试用工具，含机密说明",
        "side_effect": side_effect,
        "requires_approval": side_effect == "write",
        "input_schema": {
            "type": "object",
            "properties": {"sku_id": {"type": "string"}},
            "required": [],
            "additionalProperties": False,
        },
        "returns": "rows",
    }


class FakeRegistry:
    def __init__(self, tools: dict) -> None:
        self._tools = dict(tools)
        self.calls: list[tuple[str, dict]] = []

    def specs(self) -> list[dict]:
        return [item[0] for item in self._tools.values()]

    def call(self, name: str, arguments: dict, context) -> ToolResult:
        self.calls.append((name, arguments))
        _, function = self._tools[name]
        return ToolResult(True, data=function(arguments), meta={"tool": name})


def reply(calls: list | None = None, tokens: int = 10) -> LLMReply:
    tool_calls = [
        ToolCall(id=call_id, name=name, arguments=arguments, raw_arguments=json.dumps(arguments))
        for call_id, name, arguments in (calls or [])
    ]
    return LLMReply(
        text="",
        prompt_tokens=tokens,
        tool_calls=tool_calls,
        finish_reason="tool_calls" if tool_calls else "stop",
    )


class ScriptedClient:
    def __init__(self, replies: list[LLMReply]) -> None:
        self.replies = list(replies)
        self.seen: list[list[dict]] = []

    def chat(self, messages, **kwargs):
        if not self.replies:
            raise AssertionError("scripted client ran out of replies")
        self.seen.append(copy.deepcopy(messages))
        return self.replies.pop(0)


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = EvidenceLedger()
        self.registry = FakeRegistry(
            {
                "get_metrics": (spec("get_metrics"), lambda args: {"rows": 3}),
                "publish_listing": (spec("publish_listing", "write"), lambda args: {"ok": 1}),
                "rollback": (spec("rollback", "write"), lambda args: {"ok": 1}),
            }
        )
        self.audit: list[dict] = []
        self.gateway = ToolGateway(
            self.registry,
            AgentPolicy(role="researcher"),
            self.ledger,
            audit=self.audit.append,
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = EventStore(Path(self.tmp.name))

    def run_loop(self, replies, terminal, budget=None):
        runtime = Runtime(
            ScriptedClient(replies),
            self.gateway,
            self.ledger,
            self.store,
            budget=budget or Budget(max_steps=10),
        )
        return runtime.run(run_id="p1", system_prompt="sys", seed={}, terminal=terminal)

    def test_rejections_are_observations_and_loop_continues(self):
        """Guessing a name and asking for a hidden one are both recoverable."""
        client_replies = [
            reply(calls=[("c1", "get_weather", {})]),
            reply(calls=[("c2", "publish_listing", {"sku_id": "s1"})]),
            reply(calls=[("c3", "submit", {})]),
        ]
        client = ScriptedClient(client_replies)
        runtime = Runtime(client, self.gateway, self.ledger, self.store, budget=Budget(max_steps=10))
        result = runtime.run(
            run_id="p1a",
            system_prompt="sys",
            seed={},
            terminal={"submit": lambda args: (True, {"status": "done"}, [])},
        )
        self.assertEqual(result.status, "done")
        self.assertEqual(result.tool_calls, 2)
        # Both rejections reached the model as tool output, not as exceptions.
        self.assertIn("unknown_tool", json.dumps(client.seen[1], ensure_ascii=False))
        self.assertIn("tool_not_permitted", json.dumps(client.seen[2], ensure_ascii=False))
        self.assertEqual(self.registry.calls, [])
        # Audit separates a quality signal from a security signal.
        self.assertEqual([event["kind"] for event in self.audit], ["unknown_tool", "permission_denied"])
        self.assertEqual([event["level"] for event in self.audit], ["info", "warning"])

    def test_denial_does_not_leak_capability(self):
        """The refusal must not describe the tool, its schema, or its siblings."""
        result = self.gateway.call("publish_listing", {"sku_id": "s1"}, None)
        text = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["code"], "tool_not_permitted")
        self.assertEqual(
            result["error"], "tool not available to this role; choose from the exposed tools"
        )
        self.assertNotIn("publish_listing 测试用工具", text)
        self.assertNotIn("input_schema", text)
        self.assertNotIn("properties", text)
        self.assertNotIn("rollback", text)
        self.assertNotIn("approval", text.lower())

    def test_empty_side_effects_is_a_config_error(self):
        with self.assertRaises(ValueError):
            ToolGateway(
                self.registry,
                AgentPolicy(role="broken", side_effects=frozenset()),
                self.ledger,
            )

    def test_truncated_data_stays_a_dict_with_obs_on_top(self):
        self.registry._tools["get_metrics"] = (
            spec("get_metrics"),
            lambda args: {"blob": "x" * 4000, "ctr": 1.8},
        )
        terminal = {"submit": lambda args: (True, {"status": "done"}, [])}
        client = ScriptedClient(
            [
                reply(calls=[("c1", "get_metrics", {})]),
                reply(calls=[("c2", "submit", {})]),
            ]
        )
        runtime = Runtime(client, self.gateway, self.ledger, self.store, budget=Budget(max_steps=6))
        result = runtime.run(run_id="p3", system_prompt="sys", seed={}, terminal=terminal)
        self.assertEqual(result.status, "done")

        tool_message = next(item for item in client.seen[1] if item.get("role") == "tool")
        payload = json.loads(tool_message["content"])
        # obs sits at the top level of the observation, not inside a string.
        self.assertEqual(payload["obs"], "obs-1")
        self.assertIsInstance(payload["data"], dict)
        self.assertTrue(payload["data"]["truncated"])
        self.assertEqual(payload["data"]["dropped"], ["blob"])
        self.assertIn("hint", payload["data"])
        self.assertTrue(self.ledger.exists("obs-1"))
        self.assertEqual(self.ledger.tool_of("obs-1"), "get_metrics")

    def test_two_permission_rejections_stop_with_policy_violation(self):
        terminal_calls = {"n": 0}

        def submit(args):
            terminal_calls["n"] += 1
            return True, {"status": "done"}, []

        client = ScriptedClient(
            [
                reply(calls=[("c1", "publish_listing", {"sku_id": "s1"})]),
                reply(calls=[("c2", "publish_listing", {"sku_id": "s1"})]),
                reply(calls=[("c3", "submit", {})]),
            ]
        )
        runtime = Runtime(client, self.gateway, self.ledger, self.store, budget=Budget(max_steps=10))
        result = runtime.run(run_id="p4", system_prompt="sys", seed={}, terminal={"submit": submit})
        self.assertEqual(result.status, "policy_violation")
        self.assertIsNone(result.payload)
        # No task order left the building: the terminal tool was never reached.
        self.assertEqual(terminal_calls["n"], 0)
        self.assertFalse(any(event.get("kind") == "task_order" for event in self.store.read("p4")))
        # Reaching for a forbidden tool is a security signal: it gets one
        # reminder, because the second time the run is already over.
        nudges = [item for item in client.seen[1] if item.get("content") == POLICY_NUDGE]
        self.assertEqual(len(nudges), 1)

    def test_four_unknown_tools_stop_with_tool_misuse(self):
        """Guessing names is a competence problem, so it is labelled as one."""
        client = ScriptedClient(
            [
                reply(calls=[("u1", "get_weather", {})]),
                reply(calls=[("u2", "get_weather", {})]),
                reply(calls=[("u3", "get_weather", {})]),
                reply(calls=[("u4", "get_weather", {})]),
                reply(calls=[("u5", "submit", {})]),
            ]
        )
        runtime = Runtime(client, self.gateway, self.ledger, self.store, budget=Budget(max_steps=10))
        result = runtime.run(
            run_id="p4b",
            system_prompt="sys",
            seed={},
            terminal={"submit": lambda args: (True, {"status": "done"}, [])},
        )
        self.assertEqual(result.status, "tool_misuse")
        self.assertIsNone(result.payload)

    def test_one_violation_and_two_guesses_keep_running(self):
        """Mixed noise below both thresholds is still recoverable."""
        client = ScriptedClient(
            [
                reply(calls=[("m1", "publish_listing", {"sku_id": "s1"})]),
                reply(calls=[("m2", "get_weather", {})]),
                reply(calls=[("m3", "get_weather", {})]),
                reply(calls=[("m4", "submit", {})]),
            ]
        )
        runtime = Runtime(client, self.gateway, self.ledger, self.store, budget=Budget(max_steps=10))
        result = runtime.run(
            run_id="p4c",
            system_prompt="sys",
            seed={},
            terminal={"submit": lambda args: (True, {"status": "done"}, [])},
        )
        self.assertEqual(result.status, "done")

    def test_budget_counts_attempts_and_stops_before_execution(self):
        gateway = ToolGateway(
            self.registry,
            AgentPolicy(role="researcher"),
            self.ledger,
            max_tool_calls=1,
        )
        first = gateway.call("get_metrics", {}, None)
        second = gateway.call("get_metrics", {}, None)
        self.assertTrue(first["ok"])
        self.assertEqual(second["code"], "tool_call_budget_exceeded")
        self.assertEqual(second["error"], "tool budget exhausted; finish with the terminal tool")
        self.assertEqual(gateway.attempts, 2)
        self.assertEqual(gateway.executed, 1)
        # The budget hit was refused before the registry could run it again.
        self.assertEqual(len(self.registry.calls), 1)

        # Guessing wrong names burns attempts too: three guesses, zero executions.
        self.registry.calls.clear()
        guessing = ToolGateway(
            self.registry,
            AgentPolicy(role="researcher"),
            EvidenceLedger(),
            max_tool_calls=3,
        )
        for _ in range(3):
            self.assertEqual(guessing.call("ghost", {}, None)["code"], "unknown_tool")
        self.assertEqual(guessing.attempts, 3)
        self.assertEqual(guessing.executed, 0)
        self.assertEqual(guessing.call("ghost", {}, None)["code"], "tool_call_budget_exceeded")
        self.assertEqual(self.registry.calls, [])


if __name__ == "__main__":
    unittest.main()
