import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.agents.research import (  # noqa: E402
    SUBMIT_SCHEMA,
    ResearchError,
    build_seed,
    run_research,
    submit_schema,
)
from ops_agent.llm import LLMReply, ToolCall  # noqa: E402
from ops_agent.memory import MemoryStore  # noqa: E402
from ops_agent.quota import QuotaPolicy  # noqa: E402
from ops_agent.runtime import EventStore  # noqa: E402
from ops_agent.site_platform import (  # noqa: E402
    SiteMetrics,
    SiteProduct,
    SiteStore,
    SiteVariant,
)
from ops_agent.tools import (  # noqa: E402
    LIST_LIMIT_MAX,
    MAX_SEED_CATEGORIES,
    SEED_PAGE_ROWS,
    SEED_ROW_FIELDS,
    ToolContext,
    ToolError,
    list_products,
)

# The seed has to stay small enough to leave room for the tool schemas and the
# conversation that follows it. Measured on the serialised JSON, not a guess.
SEED_BUDGET_CHARS = 3000


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
    """A model that says the pre-scripted thing, in order."""

    def __init__(self, replies: list[LLMReply]) -> None:
        self.replies = list(replies)
        self.seen: list[list[dict]] = []
        self.kwargs: list[dict] = []
        self.enabled = True
        self.settings = SimpleNamespace(model="scripted-model")

    def chat(self, messages, **kwargs):
        if not self.replies:
            raise AssertionError("scripted client ran out of replies")
        self.seen.append([dict(item) for item in messages])
        self.kwargs.append(dict(kwargs))
        return self.replies.pop(0)

    def transcript(self) -> str:
        return json.dumps(self.seen, ensure_ascii=False, default=str)

    def tool_names(self) -> list[str]:
        return [item["function"]["name"] for item in (self.kwargs[0].get("tools") or [])]


def make_store() -> SiteStore:
    return SiteStore(
        store_id="s1",
        slug="demo-store",
        name="Demo Store",
        country="US",
        currency="USD",
        timezone="UTC",
    )


def make_product(
    product_id: str,
    *,
    category: str = "home-fragrance",
    pv: int = 1000,
    inquiries: int = 8,
    orders: int = 4,
    price_cents: int = 1999,
    cost_cents: int = 1200,
    stock: int = 100,
    negative_reviews: int = 1,
) -> SiteProduct:
    return SiteProduct(
        product_id=product_id,
        slug=product_id,
        title_en=f"Product {product_id}",
        status="active",
        category_id="c1",
        category_slug=category,
        moq=10,
        lead_time_days=15,
        incoterm="FOB",
        variants=(
            SiteVariant(
                variant_id=f"{product_id}-v1",
                sku_code=f"SKU-{product_id}",
                price_cents=price_cents,
                cost_cents=cost_cents,
                stock=stock,
            ),
        ),
        metrics=SiteMetrics(
            days=30,
            pv=pv,
            uv=max(1, pv // 10),
            inquiries=inquiries,
            orders=orders,
            units=orders,
            revenue_cents=orders * price_cents,
            reviews=3,
            avg_rating=4.2,
            negative_reviews=negative_reviews,
        ),
    )


def task_item(product_id: str, refs: list | None = None) -> dict:
    """A task order that is valid except, optionally, for its citations."""
    item = {
        "product_id": product_id,
        "problem": "low_inquiry",
        "problem_label": "询盘偏低",
        "problem_detail": "询盘率低于类目中位数，主图与标题没有说明使用场景",
        "priority": 0.8,
        "evidence": ["询盘率 0.80% 低于类目中位数 2.40%", "库存 100 件，周转 25 天"],
        "hypothesis": "若主图改为场景图并前置场景关键词，询盘率会提升 20%",
        "action": "替换主图，标题前置场景词",
        "success_metric": "inquiry_rate",
        "min_lift": 0.2,
        "window_days": 14,
        "price_low_cents": 1999,
        "price_high_cents": 2199,
        "variant_min": 2,
        "variant_max": 3,
    }
    if refs is not None:
        item["evidence_refs"] = refs
    return item


class ResearchHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.memory = MemoryStore(self.root / "memory")
        self.events = EventStore(self.root / "events")
        self.policy = QuotaPolicy()
        self.store = make_store()
        self.products = [make_product("p1"), make_product("p2"), make_product("p3")]

    def run_agent(self, replies, products=None, **kwargs):
        client = ScriptedClient(replies)
        payload = run_research(
            store=self.store,
            products=products if products is not None else self.products,
            client=client,
            memory=self.memory,
            policy=self.policy,
            run_id="run-test",
            events=self.events,
            **kwargs,
        )
        return payload, client

    def expect_failure(self, replies, products=None, **kwargs):
        client = ScriptedClient(replies)
        with self.assertRaises(ResearchError) as caught:
            run_research(
                store=self.store,
                products=products if products is not None else self.products,
                client=client,
                memory=self.memory,
                policy=self.policy,
                run_id="run-test",
                events=self.events,
                **kwargs,
            )
        return caught.exception, client

    @staticmethod
    def rejected_submits(payload: dict, times: int = 3) -> list[LLMReply]:
        return [
            reply(calls=[(f"s{index}", "submit_task_orders", payload)])
            for index in range(times)
        ]

    @staticmethod
    def rejected_declines(payload: dict, times: int = 3) -> list[LLMReply]:
        return [
            reply(calls=[(f"d{index}", "decline", payload)]) for index in range(times)
        ]

    @staticmethod
    def size(payload: dict) -> int:
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


class SubmitSchemaTests(unittest.TestCase):
    def test_ceiling_comes_from_the_store_quota(self):
        # A 120-SKU store is allowed 12 picks; the tool used to stop at 5 and
        # said nothing about it.
        self.assertEqual(submit_schema(12)["properties"]["selected"]["maxItems"], 12)
        # The module constant stays as the documented default for readers.
        self.assertEqual(SUBMIT_SCHEMA["properties"]["selected"]["maxItems"], 5)

    def test_ceiling_is_never_zero(self):
        self.assertEqual(submit_schema(0)["properties"]["selected"]["maxItems"], 1)


class SeedTests(ResearchHarness):
    def test_seed_is_a_page_with_named_columns(self):
        seed = build_seed(
            store=self.store,
            products=self.products,
            memory=self.memory,
            policy=self.policy,
        )
        # Three products fit on one page: no paging note, nothing truncated.
        self.assertEqual(seed["catalogue_size"], 3)
        self.assertEqual(len(seed["catalogue"]), 3)
        self.assertNotIn("catalogue_note", seed)
        self.assertEqual(seed["catalogue_fields"], list(SEED_ROW_FIELDS))
        # Three active products: the usual week is one, the ceiling is three.
        self.assertEqual(seed["quota"]["may_select_low"], 1)
        self.assertEqual(seed["quota"]["may_select_high"], 3)

        row = dict(zip(seed["catalogue_fields"], seed["catalogue"][0]))
        # A row is a pointer, not a dossier: details are one tool call away.
        for forbidden in ("description", "bullets", "variants", "reviews", "cost_cents"):
            self.assertNotIn(forbidden, row)
        self.assertEqual(row["flags"], [])
        self.assertLessEqual(len(row["title"]), 20)

        dead = make_product("p9", orders=0)
        dead_seed = build_seed(
            store=self.store, products=[dead], memory=self.memory, policy=self.policy
        )
        dead_row = dict(zip(dead_seed["catalogue_fields"], dead_seed["catalogue"][0]))
        self.assertIn("dead_stock", dead_row["flags"])

    def test_seed_pages_by_traffic_and_says_it_is_a_page(self):
        products = [make_product(f"p{index:02d}", pv=100 + index) for index in range(25)]
        seed = build_seed(
            store=self.store, products=products, memory=self.memory, policy=self.policy
        )
        self.assertEqual(seed["catalogue_size"], 25)
        self.assertEqual(len(seed["catalogue"]), SEED_PAGE_ROWS)
        # "Not listed" must never read as "healthy".
        self.assertIn("page 1 of 2", seed["catalogue_note"])
        self.assertIn("list_products", seed["catalogue_note"])
        pvs = [row[SEED_ROW_FIELDS.index("pv")] for row in seed["catalogue"]]
        self.assertEqual(pvs, sorted(pvs))
        self.assertLessEqual(self.size(seed), SEED_BUDGET_CHARS)

    def test_seed_categories_carry_counts_and_medians(self):
        seed = build_seed(
            store=self.store,
            products=self.products,
            memory=self.memory,
            policy=self.policy,
        )
        categories = {item["name"]: item for item in seed["categories"]}
        self.assertEqual(categories["home-fragrance"]["count"], 3)
        for key in ("count", "dead_stock", "below_floor", "median_pv"):
            self.assertIn(key, categories["home-fragrance"])
        # The median comes from benchmark_doc, not a second implementation.
        self.assertGreater(categories["home-fragrance"]["median_pv"], 0)

    def test_seed_folds_the_category_tail(self):
        products = [
            make_product(f"x{index}", category=f"cat-{index % 12:02d}") for index in range(60)
        ]
        seed = build_seed(
            store=self.store, products=products, memory=self.memory, policy=self.policy
        )
        self.assertEqual(len(seed["categories"]), MAX_SEED_CATEGORIES)
        self.assertTrue(seed["categories"][-1]["name"].startswith("+"))
        # A folded row has no median: not computed is not the same as zero.
        self.assertNotIn("median_pv", seed["categories"][-1])

    def test_seed_stays_within_the_budget_at_500_products(self):
        products = [
            make_product(f"p{index:03d}", pv=index, category=f"cat-{index % 5}")
            for index in range(500)
        ]
        seed = build_seed(
            store=self.store, products=products, memory=self.memory, policy=self.policy
        )
        self.assertEqual(seed["catalogue_size"], 500)
        self.assertEqual(len(seed["catalogue"]), SEED_PAGE_ROWS)
        self.assertLessEqual(self.size(seed), SEED_BUDGET_CHARS)


class EvidenceTests(ResearchHarness):
    def test_submit_without_evidence_refs_is_rejected(self):
        error, client = self.expect_failure(
            self.rejected_submits({"selected": [task_item("p1")]})
        )
        self.assertIn("invalid_task_orders", str(error))
        self.assertIn("need at least 2 evidence_refs", client.transcript())

    def test_submit_below_the_usual_week_needs_a_reason(self):
        # 25 SKUs at the default 10% share: the usual week is 3, so one pick has
        # to be explained rather than padded.
        products = [make_product(f"p{index:02d}", pv=100 + index) for index in range(25)]
        error, client = self.expect_failure(
            self.rejected_submits({"selected": [task_item("p1", ["obs-1", "obs-2"])]}),
            products=products,
        )
        self.assertIn("invalid_task_orders", str(error))
        self.assertIn("underfill_reason", client.transcript())

    def test_unknown_observation_is_rejected(self):
        submit = {"selected": [task_item("p1", ["obs-1", "obs-99"])]}
        replies = [reply(calls=[("c1", "get_metrics", {"product_id": "p1"})])]
        replies += self.rejected_submits(submit)
        error, client = self.expect_failure(replies)
        self.assertIn("invalid_task_orders", str(error))
        self.assertIn("unknown observation obs-99", client.transcript())

    def test_memory_hit_is_context_not_evidence(self):
        self.memory.write_lessons([{"lesson_id": "L1", "lesson": "场景图提升询盘"}])
        submit = {"selected": [task_item("p1", ["obs-1", "obs-2"])]}
        replies = [
            reply(calls=[("m1", "search_memory", {"query": "场景图", "kinds": ["lesson"]})]),
            reply(calls=[("m2", "search_memory", {"query": "场景图", "kinds": ["lesson"]})]),
        ]
        replies += self.rejected_submits(submit)
        error, client = self.expect_failure(replies)
        self.assertIn("is context, not evidence", client.transcript())

    def test_valid_submission_carries_refs_and_agent_planner(self):
        submit = {"selected": [task_item("p1", ["obs-1", "obs-2"])]}
        replies = [
            reply(calls=[("c1", "get_metrics", {"product_id": "p1"})]),
            reply(calls=[("c2", "get_product", {"product_id": "p1"})]),
            reply(calls=[("c3", "submit_task_orders", submit)]),
        ]
        payload, client = self.run_agent(replies)
        self.assertEqual(payload["planner"], "agent")
        self.assertEqual(payload["scanned"], 3)
        self.assertEqual(len(payload["selected"]), 1)
        selected = payload["selected"][0]
        self.assertEqual(selected["evidence_refs"], ["obs-1", "obs-2"])
        self.assertEqual(selected["planner"], "agent")
        self.assertEqual(selected["strategy"]["planner"], "agent")
        # The one-shot director's fields survive untouched.
        for key in ("task_id", "price_band_cents", "strategy", "score", "severity"):
            self.assertIn(key, selected)
        self.assertNotIn("status", payload)
        names = client.tool_names()
        for expected in ("get_metrics", "get_reviews", "price_calc", "submit_task_orders"):
            self.assertIn(expected, names)
        # No write tool is even offered, let alone callable.
        self.assertNotIn("publish_listing", names)
        self.assertNotIn("rollback", names)


class PerturbationTests(ResearchHarness):
    """Three ways a real store deviates from the happy path."""

    def test_sparse_category_reports_untrustworthy_median(self):
        thin = [make_product("t1", category="thin"), make_product("t2", category="thin")]
        fat = [make_product(f"f{i}", category="fat") for i in range(3)]
        replies = [
            reply(calls=[("b1", "get_benchmarks", {"category": "thin"})]),
            reply(
                calls=[
                    (
                        "d1",
                        "decline",
                        {
                            "reason": "thin 类目只有 2 个样本，中位数不可信，本周不动",
                            "evidence_refs": ["obs-1"],
                        },
                    )
                ]
            ),
        ]
        payload, client = self.run_agent(replies, products=thin + fat)
        self.assertEqual(payload["selected"], [])
        transcript = client.transcript()
        # The tool must hand the model both facts: sample size and fallback.
        self.assertIn('\\"trustworthy\\":false', transcript)
        self.assertIn('\\"scope\\":\\"store\\"', transcript)

    def test_vanishing_product_is_a_recoverable_observation(self):
        replies = [
            reply(calls=[("g1", "get_product", {"product_id": "gone"})]),
            reply(
                calls=[
                    ("g2", "get_product", {"product_id": "p1"}),
                    ("g3", "get_metrics", {"product_id": "p1"}),
                ]
            ),
            reply(
                calls=[
                    ("s1", "submit_task_orders", {"selected": [task_item("p1", ["obs-2", "obs-3"])]})
                ]
            ),
        ]
        payload, client = self.run_agent(replies)
        self.assertEqual(len(payload["selected"]), 1)
        transcript = client.transcript()
        self.assertIn('\\"code\\":\\"unknown_product\\"', transcript)
        # The dead id must not be treated as evidence either.
        self.assertEqual(payload["selected"][0]["evidence_refs"], ["obs-2", "obs-3"])

    def test_nothing_to_do_declines_instead_of_inventing_work(self):
        healthy = [make_product(f"h{i}", inquiries=90, orders=60, negative_reviews=0) for i in range(3)]
        replies = [
            reply(calls=[("o1", "get_metrics", {"product_id": "h0"})]),
            reply(
                calls=[
                    (
                        "d1",
                        "decline",
                        {"reason": "全店询盘与订单稳定，没有值得动的商品", "evidence_refs": ["obs-1"]},
                    )
                ]
            ),
        ]
        payload, _ = self.run_agent(replies, products=healthy)
        self.assertEqual(payload["selected"], [])
        self.assertEqual(payload["matched"], 0)
        self.assertIn("本周不动", payload["notes"][0])
        self.assertEqual(len(self.memory.read_decisions(limit=5)), 1)


class DeclineTests(ResearchHarness):
    def test_decline_writes_a_decision_and_no_tasks(self):
        payload, _ = self.run_agent(
            [
                reply(calls=[("o1", "get_metrics", {"product_id": "p1"})]),
                reply(
                    calls=[
                        (
                            "d1",
                            "decline",
                            {
                                "reason": "全店询盘率稳定，没有值得改的商品",
                                "evidence_refs": ["obs-1"],
                            },
                        )
                    ]
                ),
            ]
        )
        self.assertEqual(payload["selected"], [])
        self.assertEqual(payload["matched"], 0)
        self.assertIn("本周不动", payload["notes"][0])
        decisions = self.memory.read_decisions(limit=5)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision_id"], "run-test")
        self.assertEqual(decisions[0]["moved"], 0)
        # A decline must be recorded as declined, not as a normal finish.
        steps = [item for item in self.events.read("run-test") if item.get("kind") == "step"]
        self.assertEqual(steps[-1]["status"], "declined")


class DeclineEvidenceTests(ResearchHarness):
    """A decline is a claim about the store: it needs a look, not a vibe."""

    def test_decline_without_evidence_is_rejected(self):
        replies = [reply(calls=[("o1", "get_metrics", {"product_id": "p1"})])]
        replies += self.rejected_declines({"reason": "没有值得改的"})
        error, client = self.expect_failure(replies)
        self.assertIn("invalid_task_orders", str(error))
        self.assertIn("need at least 1 evidence_ref", client.transcript())

    def test_decline_with_a_memory_ref_is_rejected(self):
        self.memory.write_lessons([{"lesson_id": "L1", "lesson": "场景图提升询盘"}])
        replies = [
            reply(calls=[("m1", "search_memory", {"query": "场景图", "kinds": ["lesson"]})]),
        ]
        replies += self.rejected_declines({"reason": "记忆说上周没动", "evidence_refs": ["obs-1"]})
        error, client = self.expect_failure(replies)
        self.assertIn("is context, not evidence", client.transcript())

    def test_decline_with_a_failed_call_is_rejected(self):
        replies = [reply(calls=[("g1", "get_product", {"product_id": "gone"})])]
        replies += self.rejected_declines({"reason": "查不到东西", "evidence_refs": ["obs-1"]})
        error, client = self.expect_failure(replies)
        self.assertIn("is a failed call, not evidence", client.transcript())

    def test_decline_without_any_lookup_is_rejected(self):
        """Citing obs-1 without ever calling a tool is not evidence, it is a guess."""
        error, client = self.expect_failure(
            self.rejected_declines({"reason": "本周不动", "evidence_refs": ["obs-1"]})
        )
        self.assertIn("no product was inspected", client.transcript())

    def test_decline_records_what_it_inspected(self):
        payload, _ = self.run_agent(
            [
                reply(calls=[("o1", "get_metrics", {"product_id": "p1"})]),
                reply(calls=[("o2", "get_product", {"product_id": "p2"})]),
                reply(
                    calls=[
                        (
                            "d1",
                            "decline",
                            {"reason": "两个主力款都正常", "evidence_refs": ["obs-1"]},
                        )
                    ]
                ),
            ]
        )
        decision = self.memory.read_decisions(limit=1)[0]
        self.assertEqual(decision["inspected"], 2)
        self.assertEqual(decision["product_ids"], ["p1", "p2"])
        # The task-order file counts distinct products, not calls.
        self.assertEqual(payload["inspected"], 2)


class ListProductsTests(unittest.TestCase):
    """The discovery tool: filter, sort by one named metric, page."""

    def setUp(self):
        self.products = [
            make_product("a", category="cat-a", pv=300, orders=0),
            make_product("b", category="cat-a", pv=100),
            make_product("c", category="cat-b", pv=200),
        ]
        self.ctx = ToolContext(products=self.products)

    def test_filters_sorts_and_pages(self):
        page = list_products(self.ctx, category="cat-a", sort="pv_asc")
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["returned"], 2)
        self.assertEqual([row[0] for row in page["rows"]], ["b", "a"])
        self.assertEqual(page["fields"], list(SEED_ROW_FIELDS))

        dead = list_products(self.ctx, flag="dead_stock", sort="pv_asc")
        self.assertEqual([row[0] for row in dead["rows"]], ["a"])

        first = list_products(self.ctx, sort="pv_asc", limit=2)
        self.assertEqual([row[0] for row in first["rows"]], ["b", "c"])
        self.assertEqual(first["next_offset"], 2)
        second = list_products(self.ctx, sort="pv_asc", limit=2, offset=2)
        self.assertEqual([row[0] for row in second["rows"]], ["a"])
        self.assertNotIn("next_offset", second)

        heaviest = list_products(self.ctx, sort="pv_desc", limit=1)
        self.assertEqual(heaviest["rows"][0][0], "a")

    def test_limit_is_clamped(self):
        many = [make_product(f"p{index:02d}", pv=index) for index in range(30)]
        ctx = ToolContext(products=many)
        self.assertEqual(len(list_products(ctx, limit=99)["rows"]), LIST_LIMIT_MAX)
        self.assertEqual(len(list_products(ctx, limit=0)["rows"]), 1)
        self.assertEqual(len(list_products(ctx, limit=-3)["rows"]), 1)

    def test_unknown_category_is_empty_and_not_an_error(self):
        page = list_products(self.ctx, category="does-not-exist")
        self.assertEqual(page["total"], 0)
        self.assertEqual(page["rows"], [])
        self.assertEqual(page["returned"], 0)

    def test_sort_must_be_a_named_metric(self):
        # No blended "opportunity score": a ranking nobody can reproduce is a
        # rule-based shortlist wearing a tool's clothes.
        with self.assertRaises(ToolError):
            list_products(self.ctx, sort="opportunity_score")


class DiscoveryTests(ResearchHarness):
    def test_agent_opens_a_product_the_seed_did_not_list(self):
        products = [make_product(f"p{index:02d}", pv=100 + index) for index in range(25)]
        seed = build_seed(
            store=self.store, products=products, memory=self.memory, policy=self.policy
        )
        listed = {row[0] for row in seed["catalogue"]}
        self.assertNotIn("p24", listed)

        replies = [
            reply(calls=[("l1", "list_products", {"sort": "pv_desc", "limit": 5})]),
            reply(calls=[("g1", "get_metrics", {"product_id": "p24"})]),
            reply(calls=[("g2", "get_product", {"product_id": "p24"})]),
            reply(
                calls=[
                    (
                        "s1",
                        "submit_task_orders",
                        {
                            "selected": [task_item("p24", ["obs-2", "obs-3"])],
                            # 25 SKUs put the usual week at 3; one pick is fine but
                            # the agent has to say what the other 24 look like.
                            "underfill_reason": "其余 24 个商品流量与转化都在类目中位数附近",
                        },
                    )
                ]
            ),
        ]
        payload, client = self.run_agent(replies, products=products)
        self.assertEqual(payload["scanned"], 25)
        # One product opened, and it was not on page one of the seed.
        self.assertEqual(payload["inspected"], 1)
        self.assertEqual(payload["selected"][0]["product_id"], "p24")
        self.assertEqual(payload["selected"][0]["evidence_refs"], ["obs-2", "obs-3"])
        self.assertIn("list_products", client.tool_names())


if __name__ == "__main__":
    unittest.main()
