"""Step 4 的风险分级测试：等级表、R3 恒定、默认行为不变。

四组断言，对应 PRD 的四条验收：

1. 每个真实工具的等级快照（9 个工坊工具 + 7 个 research 只读工具）；
2. R3 永不可见——不管 `max_risk` 取什么、`policy.tools` 白名单里有没有它；
3. `max_risk=None` 与改造前逐字节一致（同样的 policy 得到同样的 schema JSON）；
4. `max_risk="R1"` 时 R1 写工具可见且 `requires_approval=True`，R2 不可见。
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.agents.base import ALLOWED_TOOL_SIDE_EFFECTS, FORBIDDEN_TOOLS  # noqa: E402
from ops_agent.runtime import AgentPolicy, EvidenceLedger, ToolGateway  # noqa: E402
from ops_agent.runtime.risk import (  # noqa: E402
    LEVELS,
    OVERRIDES,
    check_level,
    infer_risk_level,
    is_visible,
    rank,
)
from ops_agent.tools import (  # noqa: E402
    ToolRegistry,
    ToolSpec,
    build_registry,
    build_research_registry,
)

# 改造前的可见工具清单（build_registry + 只读 policy），用来做"行为不变"的锚点。
WORKSHOP_READ_ONLY = [
    "get_shop_snapshot",
    "get_competitors",
    "get_trends",
    "price_calc",
    "compliance_check",
]

RESEARCH_TOOLS = [
    "get_product",
    "get_metrics",
    "get_benchmarks",
    "get_reviews",
    "price_calc",
    "search_memory",
    "list_products",
]


def spec_dict(name: str, side_effect: str = "read", requires_approval: bool = False) -> dict:
    return {
        "name": name,
        "description": f"{name} 测试工具",
        "side_effect": side_effect,
        "requires_approval": requires_approval,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "returns": "rows",
    }


class FakeRegistry:
    """手工 registry：故意不带 risk_level，验证网关的兜底推导。"""

    def __init__(self, specs: list[dict]) -> None:
        self._specs = list(specs)

    def specs(self) -> list[dict]:
        return list(self._specs)

    def call(self, name: str, arguments: dict, context) -> None:  # pragma: no cover - 不用
        raise AssertionError("本测试只验证可见性，不调用工具")


def gateway_for(specs: list[dict], **policy_kwargs) -> ToolGateway:
    allowed = frozenset({"read", "write", "compute"})
    policy = AgentPolicy(role="risk-test", side_effects=allowed, **policy_kwargs)
    return ToolGateway(FakeRegistry(specs), policy, EvidenceLedger())


class LevelSnapshotTests(unittest.TestCase):
    """等级表是快照：改了推导规则就必须显式改这份期望，不能悄悄漂移。"""

    def test_workshop_registry_levels(self):
        levels = {item["name"]: item["risk_level"] for item in build_registry().specs()}
        self.assertEqual(
            {
                "get_shop_snapshot": "R0",
                "get_competitors": "R0",
                "get_trends": "R0",
                "price_calc": "R0",
                "generate_listing": "R0",
                "generate_assets": "R0",
                "compliance_check": "R0",
                "publish_listing": "R3",
                "rollback": "R3",
            },
            levels,
        )

    def test_research_registry_is_all_r0(self):
        levels = {item["name"]: item["risk_level"] for item in build_research_registry().specs()}
        self.assertEqual(7, len(levels))
        self.assertEqual({"R0"}, set(levels.values()))

    def test_forbidden_names_are_r3(self):
        levels = {item["name"]: item["risk_level"] for item in build_registry().specs()}
        for name in FORBIDDEN_TOOLS:
            self.assertEqual("R3", levels[name], f"{name} 必须是 R3")

    def test_no_real_tool_sits_at_r1_or_r2_today(self):
        """当前 9 个工具只有 R0 与 R3；R1/R2 是给 Step 7 的写工具预留的档位。"""

        levels = {item["risk_level"] for item in build_registry().specs()}
        self.assertEqual({"R0", "R3"}, levels)


class InferRuleTests(unittest.TestCase):
    def test_read_and_compute_are_r0(self):
        self.assertEqual("R0", infer_risk_level("anything", "read", False))
        self.assertEqual("R0", infer_risk_level("anything", "compute", False))

    def test_write_with_approval_is_r1(self):
        self.assertEqual("R1", infer_risk_level("draft_listing", "write", True))

    def test_write_without_approval_is_r2(self):
        self.assertEqual("R2", infer_risk_level("bulk_price", "write", False))

    def test_unknown_side_effect_fails_closed_to_r3(self):
        self.assertEqual("R3", infer_risk_level("mystery", "delete", False))
        self.assertEqual("R3", infer_risk_level("mystery", "", False))

    def test_forbidden_name_wins_over_a_read_label(self):
        for name in FORBIDDEN_TOOLS:
            self.assertEqual("R3", infer_risk_level(name, "read", False))
            self.assertEqual("R3", infer_risk_level(name, "compute", False))

    def test_forbidden_name_wins_over_the_override_table(self):
        """覆盖表不能把 R3 降下来：两条路径都指向 R3 才是安全的。"""

        for name in FORBIDDEN_TOOLS:
            self.assertEqual("R3", OVERRIDES.get(name, "R3"))

    def test_rank_and_check_level(self):
        self.assertEqual([0, 1, 2, 3], [rank(level) for level in LEVELS])
        self.assertEqual("R2", check_level(" r2 "))
        with self.assertRaises(ValueError):
            rank("R4")
        with self.assertRaises(ValueError):
            check_level("")

    def test_is_visible_matrix(self):
        self.assertTrue(is_visible("R0", None))
        self.assertTrue(is_visible("R1", None))
        self.assertTrue(is_visible("R2", "R3"))
        self.assertFalse(is_visible("R1", "R0"))
        self.assertTrue(is_visible("R1", "R1"))
        for max_risk in (None, "R0", "R1", "R2", "R3"):
            self.assertFalse(is_visible("R3", max_risk), f"R3 在 max_risk={max_risk} 时仍不可见")


class ToolSpecTests(unittest.TestCase):
    def test_spec_without_risk_level_is_still_constructible(self):
        spec = ToolSpec("x", "d", "read", False, {"type": "object"}, "rows")
        self.assertEqual("", spec.risk_level)
        self.assertEqual("R0", spec.to_dict()["risk_level"])

    def test_to_dict_always_reports_resolved_level(self):
        spec = ToolSpec("publish_listing", "d", "read", False, {"type": "object"}, "rows")
        # 名字在 FORBIDDEN_TOOLS 里，即便被误标成 read，输出仍是 R3。
        self.assertEqual("R3", spec.to_dict()["risk_level"])

    def test_register_freezes_the_derived_level(self):
        registry = ToolRegistry()
        draft = ToolSpec("draft_listing", "d", "write", True, {"type": "object"}, "rows")
        registry.register(draft, lambda *a: None)
        spec = registry.specs()[0]
        self.assertEqual("R1", spec["risk_level"])

    def test_register_keeps_an_explicit_level(self):
        registry = ToolRegistry()
        explicit = ToolSpec("bulk_price", "d", "write", True, {"type": "object"}, "rows", "R2")
        registry.register(explicit, lambda *a: None)
        self.assertEqual("R2", registry.specs()[0]["risk_level"])

    def test_register_rejects_a_bogus_level(self):
        registry = ToolRegistry()
        bad = ToolSpec("weird", "d", "read", False, {"type": "object"}, "rows", "R9")
        with self.assertRaises(ValueError):
            registry.register(bad, lambda *a: None)


class DefaultBehaviourUnchangedTests(unittest.TestCase):
    """`max_risk=None` 必须与改造前逐字节一致。"""

    def test_policy_without_max_risk_matches_the_pre_change_list(self):
        gateway = ToolGateway(build_registry(), AgentPolicy(role="research"), EvidenceLedger())
        self.assertEqual(WORKSHOP_READ_ONLY, gateway.allowed_names())

    def test_explicit_none_is_the_same_as_omitted(self):
        omitted = ToolGateway(build_registry(), AgentPolicy(role="research"), EvidenceLedger())
        policy = AgentPolicy(role="research", max_risk=None)
        explicit = ToolGateway(build_registry(), policy, EvidenceLedger())
        self.assertEqual(
            json.dumps(omitted.specs(), ensure_ascii=False, sort_keys=True),
            json.dumps(explicit.specs(), ensure_ascii=False, sort_keys=True),
        )

    def test_research_registry_visibility_is_unchanged(self):
        gateway = ToolGateway(build_research_registry(), AgentPolicy(role="research"), EvidenceLedger())
        self.assertEqual(RESEARCH_TOOLS, gateway.allowed_names())

    def test_old_side_effect_and_forbidden_sets_are_untouched(self):
        # 这两个集合是既有安全边界，本步只允许在旁边加 max_risk，不许改它们。
        self.assertEqual(frozenset({"read", "compute"}), ALLOWED_TOOL_SIDE_EFFECTS)
        self.assertEqual(frozenset({"publish_listing", "rollback"}), FORBIDDEN_TOOLS)


class MaxRiskGatingTests(unittest.TestCase):
    """用合成工具证明闸门确实在动：真实 registry 里没有 R1/R2 工具。"""

    def setUp(self) -> None:
        self.specs = [
            spec_dict("read_metrics"),
            spec_dict("compute_score", "compute"),
            spec_dict("draft_listing", "write", requires_approval=True),  # R1
            spec_dict("bulk_price", "write", requires_approval=False),     # R2
            spec_dict("publish_listing", "write", requires_approval=True),  # R3
        ]

    def test_none_exposes_everything_except_r3(self):
        names = gateway_for(self.specs).allowed_names()
        self.assertEqual(["read_metrics", "compute_score", "draft_listing", "bulk_price"], names)

    def test_r1_hides_batch_writes_but_keeps_single_writes(self):
        names = gateway_for(self.specs, max_risk="R1").allowed_names()
        self.assertEqual(["read_metrics", "compute_score", "draft_listing"], names)

    def test_r0_hides_every_write(self):
        names = gateway_for(self.specs, max_risk="R0").allowed_names()
        self.assertEqual(["read_metrics", "compute_score"], names)

    def test_r1_write_tool_still_requires_approval(self):
        gateway = gateway_for(self.specs, max_risk="R1")
        self.assertIn("draft_listing", gateway.allowed_names())
        visible = {item["function"]["name"] for item in gateway.specs()}
        self.assertIn("draft_listing", visible)
        raw = {item["name"]: item for item in gateway.registry.specs()}
        self.assertTrue(raw["draft_listing"]["requires_approval"])

    def test_r3_hidden_even_when_named_in_the_allowlist(self):
        # 白名单只写 R3：过滤后一个都不剩，构造期就报错——而不是给模型一个空列表。
        with self.assertRaises(ValueError):
            ToolGateway(
                FakeRegistry(self.specs),
                AgentPolicy(role="x", side_effects=frozenset({"read", "write"}), max_risk="R3",
                            tools=frozenset({"publish_listing"})),
                EvidenceLedger(),
            )
        # 白名单同时写 R3 和 R0：R3 依然进不来。
        gateway = gateway_for(self.specs, tools=frozenset({"publish_listing", "read_metrics"}))
        self.assertEqual(["read_metrics"], gateway.allowed_names())

    def test_bad_max_risk_is_rejected_at_policy_construction(self):
        with self.assertRaises(ValueError):
            AgentPolicy(role="x", max_risk="R4")
        with self.assertRaises(ValueError):
            AgentPolicy(role="x", max_risk="")

    def test_max_risk_is_normalised(self):
        self.assertEqual("R1", AgentPolicy(role="x", max_risk=" r1 ").max_risk)

    def test_gateway_recomputes_when_policy_changes(self):
        """缓存不失效是刻意的：换上限必须换实例，这里把这个约定钉住。"""

        narrow = gateway_for(self.specs, max_risk="R0")
        wide = gateway_for(self.specs, max_risk="R2")
        self.assertLess(len(narrow.allowed_names()), len(wide.allowed_names()))


if __name__ == "__main__":
    unittest.main()
