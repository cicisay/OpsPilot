"""意图层 Step 1 的单元测试：路由规则 + 规格表契约。

覆盖 PRD 要求的三类：
1. 12 个意图各一条样例；
2. 边界（空串 / 纯空白 / None / 超长 / 歧义 / 复合委托）；
3. 失败路径（不许读文件、不许凭猜测补槽位、置信度越界要报错）。
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.intake import (  # noqa: E402
    INTENT_SPECS,
    PLANNED_TOOLS,
    RISK_LEVELS,
    Intent,
    IntentResult,
    classify,
    explain,
    normalize,
)
from ops_agent.tools import build_registry, build_research_registry  # noqa: E402


# 12 类意图各一条"像人会说的话"，同时充当 Step 2 语料的第一批种子。
SAMPLES = {
    Intent.BATCH_UPLOAD: "这20个新品帮我上到美国站",
    Intent.SINGLE_UPLOAD: "把这个产品上架到德国站",
    Intent.LISTING_DIAGNOSE: "帮我诊断一下 SKU-0007 为什么转化差",
    Intent.LISTING_REWRITE: "把这条链接的标题重写一下，突出防水卖点",
    Intent.MULTILANG_CONTENT: "把五点描述翻译成德语",
    Intent.MEDIA_COMPOSE: "给这个新品做一套主图和场景图",
    Intent.DATA_QUERY: "上周美国站销售额和转化率是多少",
    Intent.MEMORY_MANAGE: "记住，以后宠物类目一律不做",
    Intent.PRICE_SUGGEST: "这个产品定价多少合适，帮我看下价格带",
    Intent.CUSTOMER_OUTREACH: "给这个买家写一封催评邮件",
    Intent.CLARIFY: "这个产品的价格和销量怎么样",
    Intent.UNKNOWN: "今天天气不错",
}


class TestIntentSchema(unittest.TestCase):
    """规格表是 Step 4 / Step 5 的输入，形状必须锁死。"""

    def test_twelve_intents_declared(self) -> None:
        self.assertEqual(12, len(Intent))
        self.assertEqual(set(Intent), set(INTENT_SPECS))

    def test_spec_keys_are_whitelisted(self) -> None:
        allowed = {"slots", "artifact", "toolset", "risk_ceiling", "owner"}
        for intent, spec in INTENT_SPECS.items():
            with self.subTest(intent=intent.value):
                self.assertEqual(set(), set(spec) - allowed)
                # 角色不是意图的属性：工具授权只发生在 research 一处。
                self.assertNotIn("role", spec)

    def test_batch_upload_slots_match_prd(self) -> None:
        self.assertEqual(
            ["source_path", "market", "language"],
            INTENT_SPECS[Intent.BATCH_UPLOAD]["slots"],
        )

    def test_risk_ceiling_values(self) -> None:
        for intent, spec in INTENT_SPECS.items():
            with self.subTest(intent=intent.value):
                self.assertIn(spec["risk_ceiling"], RISK_LEVELS)
                # 发布/回滚永不在意图可达范围内，所以天花板不允许出现 R2/R3。
                self.assertIn(spec["risk_ceiling"], ("R0", "R1"))

    def test_toolset_refers_to_real_or_planned_tools(self) -> None:
        known = {spec["name"] for spec in build_registry().specs()}
        known |= {spec["name"] for spec in build_research_registry().specs()}
        declared = {name for spec in INTENT_SPECS.values() for name in spec["toolset"]}
        unknown = declared - known - PLANNED_TOOLS
        self.assertEqual(set(), unknown, f"声明了不存在的工具: {sorted(unknown)}")

    def test_intent_result_rejects_bad_confidence(self) -> None:
        with self.assertRaises(ValueError):
            IntentResult(intent=Intent.DATA_QUERY, confidence=1.5)
        with self.assertRaises(ValueError):
            IntentResult(intent="DATA_QUERY")  # type: ignore[arg-type]


class TestIntentRouting(unittest.TestCase):
    """12 类意图的识别结果。"""

    def test_batch_upload(self) -> None:
        result = classify(SAMPLES[Intent.BATCH_UPLOAD])
        self.assertEqual(Intent.BATCH_UPLOAD, result.intent)
        self.assertEqual("US", result.slots["market"])
        self.assertEqual(20, result.slots["quantity"])
        # 文里没给来源文件、没说语言，必须如实报缺。
        self.assertIn("source_path", result.missing)
        self.assertIn("language", result.missing)
        self.assertGreaterEqual(result.confidence, 0.8)

    def test_single_upload(self) -> None:
        result = classify(SAMPLES[Intent.SINGLE_UPLOAD])
        self.assertEqual(Intent.SINGLE_UPLOAD, result.intent)
        self.assertEqual("DE", result.slots["market"])
        self.assertIn("sku", result.missing)

    def test_listing_diagnose(self) -> None:
        result = classify(SAMPLES[Intent.LISTING_DIAGNOSE])
        self.assertEqual(Intent.LISTING_DIAGNOSE, result.intent)
        self.assertEqual("SKU-0007", result.slots["sku"])
        self.assertEqual("转化差", result.slots["problem"])
        self.assertEqual([], result.missing)

    def test_listing_rewrite(self) -> None:
        result = classify(SAMPLES[Intent.LISTING_REWRITE])
        self.assertEqual(Intent.LISTING_REWRITE, result.intent)
        self.assertEqual("标题", result.slots["field"])

    def test_multilang_content(self) -> None:
        result = classify(SAMPLES[Intent.MULTILANG_CONTENT])
        self.assertEqual(Intent.MULTILANG_CONTENT, result.intent)
        self.assertEqual(["de"], result.slots["languages"])

    def test_media_compose(self) -> None:
        result = classify(SAMPLES[Intent.MEDIA_COMPOSE])
        self.assertEqual(Intent.MEDIA_COMPOSE, result.intent)
        self.assertEqual(["主图", "场景图"], result.slots["asset_types"])

    def test_data_query(self) -> None:
        result = classify(SAMPLES[Intent.DATA_QUERY])
        self.assertEqual(Intent.DATA_QUERY, result.intent)
        self.assertEqual(["销售额", "转化率"], result.slots["metrics"])

    def test_memory_manage(self) -> None:
        result = classify(SAMPLES[Intent.MEMORY_MANAGE])
        self.assertEqual(Intent.MEMORY_MANAGE, result.intent)
        self.assertEqual("add", result.slots["action"])
        self.assertIn("宠物", result.slots["content"])

    def test_price_suggest(self) -> None:
        result = classify(SAMPLES[Intent.PRICE_SUGGEST])
        self.assertEqual(Intent.PRICE_SUGGEST, result.intent)
        self.assertIn("sku", result.missing)

    def test_customer_outreach_is_declared_but_has_no_tools(self) -> None:
        result = classify(SAMPLES[Intent.CUSTOMER_OUTREACH])
        self.assertEqual(Intent.CUSTOMER_OUTREACH, result.intent)
        self.assertEqual("催评", result.slots["topic"])
        # 本轮不实现客户触达（PRD §8），工具集必须是空的。
        self.assertEqual([], INTENT_SPECS[Intent.CUSTOMER_OUTREACH]["toolset"])

    def test_ambiguous_request_asks_for_clarification(self) -> None:
        result = classify(SAMPLES[Intent.CLARIFY])
        self.assertEqual(Intent.CLARIFY, result.intent)
        self.assertLess(result.confidence, 0.6)
        self.assertEqual(2, len(result.candidates))
        self.assertIn("DATA_QUERY", result.candidates)
        self.assertIn("PRICE_SUGGEST", result.candidates)

    def test_noise_returns_unknown(self) -> None:
        result = classify(SAMPLES[Intent.UNKNOWN])
        self.assertEqual(Intent.UNKNOWN, result.intent)
        self.assertEqual(0.0, result.confidence)
        self.assertEqual({}, result.slots)

    def test_all_twelve_intents_are_reachable(self) -> None:
        produced = {classify(text).intent for text in SAMPLES.values()}
        self.assertEqual(set(Intent), produced)


class TestBoundaries(unittest.TestCase):
    """边界输入必须有明确返回值，且不能抛异常。"""

    def test_empty_string(self) -> None:
        result = classify("")
        self.assertEqual(Intent.UNKNOWN, result.intent)
        self.assertEqual(0.0, result.confidence)
        self.assertEqual([], result.missing)

    def test_whitespace_only(self) -> None:
        self.assertEqual(Intent.UNKNOWN, classify("   \n\t  ").intent)

    def test_none_input(self) -> None:
        result = classify(None)  # type: ignore[arg-type]
        self.assertEqual(Intent.UNKNOWN, result.intent)

    def test_overlong_text_keeps_raw_but_matches_prefix(self) -> None:
        text = "这20个新品帮我上到美国站" + "。" * 3000
        result = classify(text)
        self.assertEqual(Intent.BATCH_UPLOAD, result.intent)
        # 匹配只看前 2000 字，但原文不能丢——否则日志里查不到用户到底说了什么。
        self.assertEqual(len(text), len(result.raw_text))

    def test_overlong_pure_noise_does_not_raise(self) -> None:
        result = classify("哈" * 5000)
        self.assertEqual(Intent.UNKNOWN, result.intent)

    def test_composite_delegation_keeps_primary_and_lists_others(self) -> None:
        result = classify("先把这个产品上架，再把标题优化一下")
        # 主意图跟在文本里先出现的那个走，另一件事进 candidates 供前端提示。
        self.assertEqual(Intent.SINGLE_UPLOAD, result.intent)
        self.assertIn("LISTING_REWRITE", result.candidates)
        self.assertGreaterEqual(result.confidence, 0.6)

    def test_confidence_always_in_range(self) -> None:
        for text in list(SAMPLES.values()) + ["", "???", "上", "把东西弄一下"]:
            with self.subTest(text=text):
                result = classify(text)
                self.assertGreaterEqual(result.confidence, 0.0)
                self.assertLessEqual(result.confidence, 1.0)

    def test_vague_verb_with_copy_and_media_parts_asks_first(self) -> None:
        # K-04：一句话点了"标题"和"图片"两件不同的活，动作却是"弄"。
        # 以前按素材分高直接派 MEDIA_COMPOSE，等于替用户决定先做图。
        result = classify("帮我弄弄这个标题和图片")
        self.assertEqual(Intent.CLARIFY, result.intent)
        self.assertIn("MEDIA_COMPOSE", result.candidates)

    def test_single_part_with_vague_verb_is_not_downgraded(self) -> None:
        # 只有一个部位时不能连带被打成 CLARIFY：单说图片仍然该判 MEDIA。
        self.assertEqual(Intent.MEDIA_COMPOSE, classify("帮我弄一下这个图片").intent)
        self.assertEqual(Intent.LISTING_REWRITE, classify("把标题重写一下").intent)

    def test_liquidity_symptoms_are_diagnose(self) -> None:
        # 用户实测词典缺口："滞销"不在症状词表，"检查"只有 0.6，直接掉 CLARIFY。
        result = classify("帮我检查店铺滞销商品")
        self.assertEqual(Intent.LISTING_DIAGNOSE, result.intent)
        self.assertGreaterEqual(result.confidence, 0.6)

    def test_shop_word_is_not_an_upload_verb(self) -> None:
        # "店铺"的"铺"不是"铺货"。负向后顾挡住"店铺"，裸"铺/铺货"照常生效。
        self.assertNotEqual(Intent.BATCH_UPLOAD, classify("这个店铺怎么样").intent)
        self.assertNotEqual(Intent.BATCH_UPLOAD, classify("这个公司怎么样").intent)
        self.assertEqual(Intent.BATCH_UPLOAD, classify("铺货到美国站").intent)
        self.assertEqual(Intent.BATCH_UPLOAD, classify("帮我铺这批货").intent)

    def test_checking_shop_binds_diagnose_even_with_typos(self) -> None:
        # "检查 + 对象"是完整诊断动作；打字变形（滞后销）不命中症状词也要接得住。
        result = classify("帮我检查店铺滞后销商品")
        self.assertEqual(Intent.LISTING_DIAGNOSE, result.intent)
        self.assertGreaterEqual(result.confidence, 0.6)

    def test_asking_for_a_list_is_data_query_not_diagnose(self) -> None:
        # 问名单是查数；但"三十几个款"是数量，不能被"问清单"抢成 DATA_QUERY。
        self.assertEqual(Intent.DATA_QUERY, classify("店里哪些商品滞销").intent)
        self.assertEqual(
            Intent.BATCH_UPLOAD,
            classify("供应商发了个表格，里面三十几个款，帮我上架到德国站").intent,
        )


class TestSlotContract(unittest.TestCase):
    """槽位只放证据，缺什么如实报；默认值不许污染 slots。"""

    def test_slots_contain_no_invented_values(self) -> None:
        result = classify("帮我把这个链接上架")
        self.assertNotIn("market", result.slots)
        self.assertNotIn("language", result.slots)
        self.assertIn("market", result.missing)

    def test_defaults_suppress_missing_without_polluting_slots(self) -> None:
        defaults = {"market": "US", "language": "en"}
        result = classify("这20个新品帮我上架", defaults=defaults)
        self.assertNotIn("market", result.missing)
        self.assertNotIn("language", result.missing)
        # 关键：默认值只压 missing，不写进 slots，Step 3 才能标 assumed。
        self.assertNotIn("market", result.slots)
        self.assertNotIn("language", result.slots)

    def test_missing_only_covers_declared_slots(self) -> None:
        result = classify("这20个新品帮我上到美国站")
        spec_slots = set(INTENT_SPECS[result.intent]["slots"])
        self.assertTrue(set(result.missing) <= spec_slots)

    def test_extra_slots_are_kept_for_mapping(self) -> None:
        result = classify("这20个新品帮我上到美国站")
        # quantity 不在规格表里，但 Step 3 拆单要用，不能丢。
        self.assertEqual(20, result.slots["quantity"])

    def test_clarify_carries_no_missing_slots(self) -> None:
        result = classify(SAMPLES[Intent.CLARIFY])
        self.assertEqual(Intent.CLARIFY, result.intent)
        self.assertEqual([], result.missing)

    def test_own_sku_code_is_extracted_as_is(self) -> None:
        # K-01：卖家自有编码（FT-001）不是 SKU-数字 形式，以前抽不到，只能靠计划层兜底。
        result = classify("把 FT-001 的标题重写一下")
        self.assertEqual("FT-001", result.slots["sku"])
        self.assertNotIn("sku", result.missing)

    def test_sku_prefix_still_works_and_is_normalized(self) -> None:
        # 老写法不能退化：大小写、连字符/下划线都要归一。
        self.assertEqual("SKU-123", classify("sku-123 的标题改一下").slots["sku"])
        self.assertEqual("FT-002", classify("把 ft_002 翻译成德语").slots["sku"])

    def test_date_like_token_is_not_taken_as_sku(self) -> None:
        # 只认"2 个以上字母 + 连字符 + 2 位以上数字"，年月写法不能被当成货号。
        self.assertNotIn("sku", classify("2024-01 这个月的销量多少").slots)


class TestRobustness(unittest.TestCase):
    """输入脏数据时不崩、不误判。"""

    def test_fullwidth_characters_normalized(self) -> None:
        result = classify("这２０个新品帮我上到美国站")
        self.assertEqual(Intent.BATCH_UPLOAD, result.intent)
        self.assertEqual(20, result.slots["quantity"])

    def test_zero_width_characters_do_not_break_matching(self) -> None:
        result = classify("这20个新品帮我上到美国\u200b站")
        self.assertEqual(Intent.BATCH_UPLOAD, result.intent)
        self.assertEqual("US", result.slots["market"])

    def test_normalize_keeps_meaning(self) -> None:
        self.assertEqual("这20个新品 上架", normalize("  这２０个新品\u200b   上架 "))

    def test_classify_is_deterministic(self) -> None:
        text = SAMPLES[Intent.BATCH_UPLOAD]
        first, second = classify(text), classify(text)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_router_never_touches_the_filesystem(self) -> None:
        # 本步禁止在 router 里读文件；用 mock 把 open 封死，能跑通就说明没读。
        with mock.patch("builtins.open", side_effect=AssertionError("router 不许读文件")):
            for text in SAMPLES.values():
                classify(text)

    def test_explain_reports_which_rules_fired(self) -> None:
        detail = explain(SAMPLES[Intent.BATCH_UPLOAD])
        self.assertIn("BATCH_UPLOAD", detail)
        self.assertIn("批量限定 + 上传动作", detail["BATCH_UPLOAD"])

    def test_result_is_json_serializable(self) -> None:
        result = classify(SAMPLES[Intent.BATCH_UPLOAD])
        # 验收命令就是 json.dumps(result.__dict__)，这里锁住这个用法。
        dumped = json.dumps(result.__dict__, ensure_ascii=False, default=str)
        self.assertIn("BATCH_UPLOAD", dumped)
        payload = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))
        self.assertEqual("BATCH_UPLOAD", payload["intent"])
        self.assertEqual(0.95, payload["confidence"])


if __name__ == "__main__":
    unittest.main()
