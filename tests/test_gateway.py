import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.runtime import AgentPolicy, EvidenceLedger, ToolGateway  # noqa: E402
from ops_agent.tools import ToolResult  # noqa: E402


def spec(name: str, side_effect: str = "read") -> dict:
    return {
        "name": name,
        "description": f"{name} 测试用工具，含机密说明",
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
    """Hand-written stub: the gateway must not depend on build_registry()."""

    def __init__(self, tools: dict) -> None:
        self._tools = dict(tools)
        self.calls: list[tuple[str, dict]] = []

    def specs(self) -> list[dict]:
        return [item[0] for item in self._tools.values()]

    def call(self, name: str, arguments: dict, context) -> ToolResult:
        self.calls.append((name, arguments))
        _, function = self._tools[name]
        return ToolResult(True, data=function(arguments), meta={"tool": name})


class GatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = EvidenceLedger()
        self.registry = FakeRegistry(
            {
                "get_metrics": (spec("get_metrics"), lambda args: {"rows": 3}),
                "get_reviews": (spec("get_reviews"), lambda args: {"rows": 1}),
                "score_listing": (spec("score_listing", "compute"), lambda args: {"score": 0.8}),
                "publish_listing": (spec("publish_listing", "write"), lambda args: {"ok": 1}),
                "rollback": (spec("rollback", "write"), lambda args: {"ok": 1}),
            }
        )
        self.gateway = ToolGateway(self.registry, AgentPolicy(role="researcher"), self.ledger)

    def test_write_and_compute_tools_are_invisible(self):
        """A read-only policy exposes read tools only, in OpenAI schema shape."""
        specs = self.gateway.specs()
        names = [item["function"]["name"] for item in specs]
        self.assertEqual(names, ["get_metrics", "get_reviews"])
        self.assertNotIn("publish_listing", names)
        self.assertNotIn("rollback", names)
        self.assertNotIn("score_listing", names)
        first = specs[0]
        self.assertEqual(first["type"], "function")
        self.assertEqual(set(first["function"]), {"name", "description", "parameters"})
        self.assertEqual(first["function"]["parameters"]["type"], "object")

    def test_unknown_tool_is_an_observation_not_an_exception(self):
        """A hallucinated tool name is recoverable: feed it back, never call out."""
        result = self.gateway.call("get_weather", {"city": "Berlin"}, None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["obs"], "")
        self.assertEqual(result["code"], "unknown_tool")
        self.assertEqual(result["error"], "unknown tool: get_weather")
        self.assertEqual(self.registry.calls, [])

    def test_out_of_policy_tool_returns_denied_observation(self):
        """Existing but out of scope is a model mistake: a neutral 'no', no schema."""
        result = self.gateway.call("publish_listing", {"sku_id": "s1"}, None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "tool_not_permitted")
        self.assertEqual(result["error"], "tool not available to this role; choose from the exposed tools")
        self.assertEqual(self.registry.calls, [])
        # Capability leak check: no description, no input schema, no siblings.
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("input_schema", text)
        self.assertNotIn("机密说明", text)
        self.assertNotIn("rollback", text)

    def test_each_call_gets_an_incrementing_obs_id(self):
        first = self.gateway.call("get_metrics", {"sku_id": "s1"}, None)
        second = self.gateway.call("get_reviews", {"sku_id": "s1"}, None)
        self.assertEqual(first["obs"], "obs-1")
        self.assertEqual(second["obs"], "obs-2")
        self.assertEqual(self.ledger.tool_of("obs-1"), "get_metrics")
        self.assertEqual(self.ledger.get("obs-1")["result"]["data"], {"rows": 3})
        self.assertTrue(self.ledger.exists("obs-2"))
        self.assertEqual(self.ledger.digest(), "2 observations: get_metrics x1, get_reviews x1")

    def test_long_result_is_truncated_but_still_addressable(self):
        self.registry._tools["get_metrics"] = (spec("get_metrics"), lambda args: {"blob": "x" * 4000})
        result = self.gateway.call("get_metrics", {}, None)
        data = result["data"]
        # The model-facing payload stays a dict with an actionable envelope.
        self.assertIsInstance(data, dict)
        self.assertTrue(data["truncated"])
        self.assertEqual(data["dropped"], ["blob"])
        self.assertTrue(data["preview"].startswith("{"))
        self.assertIn("hint", data)
        self.assertLessEqual(len(json.dumps(data, ensure_ascii=False)), 1500)
        # The id still resolves even though the body was shaped.
        self.assertTrue(self.ledger.exists(result["obs"]))
        self.assertEqual(self.ledger.tool_of(result["obs"]), "get_metrics")
        self.assertEqual(self.ledger.result(result["obs"])["data"]["dropped"], ["blob"])

    def test_to_dict_round_trip_preserves_ids(self):
        self.gateway.call("get_metrics", {"sku_id": "s1"}, None)
        self.gateway.call("get_reviews", {"sku_id": "s2"}, None)
        payload = json.loads(json.dumps(self.ledger.to_dict(), ensure_ascii=False))
        for item in payload["items"].values():
            # Disk holds digests and a summary, never the payload itself.
            self.assertEqual(
                set(item),
                {"obs_id", "tool", "args_sha1", "result_sha1", "summary", "numeric_facts"},
            )
        restored = EvidenceLedger.from_dict(payload)
        for obs_id in ("obs-1", "obs-2"):
            self.assertTrue(restored.exists(obs_id))
            self.assertEqual(restored.tool_of(obs_id), self.ledger.tool_of(obs_id))
            self.assertEqual(restored.get(obs_id)["args_sha1"], self.ledger.get(obs_id)["args_sha1"])
            self.assertEqual(restored.summary_of(obs_id), self.ledger.summary_of(obs_id))
        self.assertEqual(restored.digest(), self.ledger.digest())
        # The next id after a reload must not collide with an existing one.
        self.assertEqual(restored.add("get_metrics", {}, {"ok": True}), "obs-3")


if __name__ == "__main__":
    unittest.main()
