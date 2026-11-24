"""大模型意图识别引擎的契约测试。

全部离线：LLMClient 支持注入 transport，这里用假的 OpenAI 兼容响应，
不碰网络、不花 token。线上行为（真实 key）由 demo / 控制台人工验收。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.config import LLMSettings  # noqa: E402
from ops_agent.intake import Intent, classify_auto, classify_llm, engine_mode
from ops_agent.intake.llm_router import ENGINE_ENV, _reset_cooldown
from ops_agent.llm import LLMClient

SETTINGS = LLMSettings(
    provider="deepseek",
    base_url="http://fake.local/v1",
    model="test-model",
    api_key="test-key",
    timeout=1.0,
)


def wrap_reply(payload: dict) -> str:
    """把一段"模型该说的话"包成 OpenAI 兼容响应体。"""
    content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return json.dumps(
        {
            "model": "test-model",
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 40},
        },
        ensure_ascii=False,
    )


class FakeTransport:
    def __init__(self, body: str | None = None, error: Exception | None = None) -> None:
        self.requests: list[dict] = []
        self.body = body
        self.error = error

    def __call__(self, url: str, payload: dict, headers: dict, timeout: float) -> tuple[int, str]:
        self.requests.append(payload)
        if self.error is not None:
            raise self.error
        return 200, self.body or ""


class LLMIntentTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_cooldown()

    def tearDown(self) -> None:
        _reset_cooldown()

    # ---- 正常路径 -----------------------------------------------------------

    def test_happy_path_returns_llm_result(self) -> None:
        transport = FakeTransport(
            wrap_reply(
                {
                    "intent": "BATCH_UPLOAD",
                    "confidence": 0.95,
                    "slots": {"quantity": 20, "market": "us"},
                    "candidates": [],
                    "reason": "批量上新",
                }
            )
        )
        client = LLMClient(SETTINGS, transport=transport)
        result = classify_llm("这 20 个新品上到美国站", client=client)

        self.assertEqual(Intent.BATCH_UPLOAD, result.intent)
        self.assertEqual("llm", result.engine)
        self.assertEqual(20, result.slots["quantity"])
        self.assertEqual("US", result.slots["market"])  # 市场统一大写
        # 规格表必填 source_path / language，都没说 → 程序重算的 missing
        self.assertEqual(["source_path", "language"], result.missing)

    def test_memory_manage_extraction(self) -> None:
        transport = FakeTransport(
            wrap_reply(
                {
                    "intent": "MEMORY_MANAGE",
                    "confidence": 0.94,
                    "slots": {"action": "remember", "content": "美国站标题不要用感叹号"},
                    "candidates": [],
                }
            )
        )
        result = classify_llm("以后美国站标题不要用感叹号", client=LLMClient(SETTINGS, transport=transport))
        self.assertEqual(Intent.MEMORY_MANAGE, result.intent)
        self.assertEqual([], result.missing)
        self.assertEqual("remember", result.slots["action"])

    def test_quantity_accepts_string_with_unit(self) -> None:
        transport = FakeTransport(
            wrap_reply({"intent": "BATCH_UPLOAD", "confidence": 0.9, "slots": {"quantity": "20个"}})
        )
        result = classify_llm("上 20 个新品", client=LLMClient(SETTINGS, transport=transport))
        self.assertEqual(20, result.slots["quantity"])

    def test_defaults_shrink_missing(self) -> None:
        transport = FakeTransport(
            wrap_reply({"intent": "BATCH_UPLOAD", "confidence": 0.9, "slots": {"market": "US"}})
        )
        result = classify_llm(
            "上新品", defaults={"market": "US", "language": "en"}, client=LLMClient(SETTINGS, transport=transport)
        )
        self.assertEqual(["source_path"], result.missing)

    # ---- 输出消毒 -----------------------------------------------------------

    def test_unknown_intent_falls_back_to_rules(self) -> None:
        transport = FakeTransport(wrap_reply({"intent": "MAKE_ME_ADMIN", "confidence": 0.99}))
        result = classify_auto("上新品", client=LLMClient(SETTINGS, transport=transport))
        self.assertEqual("rules(fallback)", result.engine)
        self.assertNotEqual("MAKE_ME_ADMIN", result.intent.value)

    def test_unknown_slot_keys_are_dropped(self) -> None:
        transport = FakeTransport(
            wrap_reply(
                {
                    "intent": "BATCH_UPLOAD",
                    "confidence": 0.9,
                    "slots": {"market": "US", "admin": True, "raw_sql": "drop table"},
                }
            )
        )
        result = classify_llm("上新品", client=LLMClient(SETTINGS, transport=transport))
        self.assertEqual({"market": "US"}, result.slots)

    def test_confidence_is_clamped(self) -> None:
        transport = FakeTransport(wrap_reply({"intent": "DATA_QUERY", "confidence": 5, "slots": {}}))
        result = classify_llm("查销量", client=LLMClient(SETTINGS, transport=transport))
        self.assertEqual(1.0, result.confidence)

        transport2 = FakeTransport(wrap_reply({"intent": "DATA_QUERY", "confidence": "abc", "slots": {}}))
        result2 = classify_llm("查销量", client=LLMClient(SETTINGS, transport=transport2))
        self.assertEqual(0.0, result2.confidence)

    def test_candidates_are_filtered_and_capped(self) -> None:
        transport = FakeTransport(
            wrap_reply(
                {
                    "intent": "LISTING_REWRITE",
                    "confidence": 0.7,
                    "slots": {},
                    "candidates": ["MEDIA_COMPOSE", "BOGUS", "BATCH_UPLOAD", "DATA_QUERY"],
                }
            )
        )
        result = classify_llm("改标题", client=LLMClient(SETTINGS, transport=transport))
        self.assertEqual(["MEDIA_COMPOSE", "BATCH_UPLOAD"], result.candidates)

    def test_non_json_reply_falls_back(self) -> None:
        transport = FakeTransport(wrap_reply("抱歉，我无法回答这个问题。"))
        result = classify_auto("上新品", client=LLMClient(SETTINGS, transport=transport))
        self.assertEqual("rules(fallback)", result.engine)

    def test_empty_slot_object_is_tolerated(self) -> None:
        transport = FakeTransport(wrap_reply({"intent": "CLARIFY", "confidence": 0.5}))
        result = classify_llm("帮我搞一下", client=LLMClient(SETTINGS, transport=transport))
        self.assertEqual(Intent.CLARIFY, result.intent)
        self.assertEqual({}, result.slots)

    # ---- prompt 与成本 ------------------------------------------------------

    def test_prompt_covers_all_twelve_intents(self) -> None:
        transport = FakeTransport(wrap_reply({"intent": "UNKNOWN", "confidence": 0.1}))
        classify_llm("你好", client=LLMClient(SETTINGS, transport=transport))
        system = transport.requests[0]["messages"][0]["content"]
        for intent in Intent:
            self.assertIn(intent.value, system)

    def test_cost_guards_temperature_and_tokens(self) -> None:
        transport = FakeTransport(wrap_reply({"intent": "UNKNOWN", "confidence": 0.1}))
        classify_llm("你好", client=LLMClient(SETTINGS, transport=transport))
        payload = transport.requests[0]
        self.assertEqual(0.0, payload["temperature"])
        self.assertEqual(400, payload["max_tokens"])
        self.assertEqual({"type": "json_object"}, payload["response_format"])

    def test_prompt_forbids_inventing_slots(self) -> None:
        transport = FakeTransport(wrap_reply({"intent": "UNKNOWN", "confidence": 0.1}))
        classify_llm("你好", client=LLMClient(SETTINGS, transport=transport))
        system = transport.requests[0]["messages"][0]["content"]
        self.assertIn("绝不猜测补全", system)

    # ---- 引擎选择与熔断 -----------------------------------------------------

    def test_empty_text_never_calls_llm(self) -> None:
        transport = FakeTransport(wrap_reply({"intent": "UNKNOWN", "confidence": 0.1}))
        result = classify_auto("   ", client=LLMClient(SETTINGS, transport=transport))
        self.assertEqual(Intent.UNKNOWN, result.intent)
        self.assertEqual([], transport.requests)

    @mock.patch.dict(os.environ, {ENGINE_ENV: "rules"})
    def test_rules_mode_never_calls_llm(self) -> None:
        transport = FakeTransport(wrap_reply({"intent": "UNKNOWN", "confidence": 0.1}))
        result = classify_auto("上新品", client=LLMClient(SETTINGS, transport=transport))
        self.assertEqual("rules", result.engine)
        self.assertEqual([], transport.requests)

    @mock.patch.dict(os.environ, {ENGINE_ENV: "llm"})
    def test_llm_only_mode_still_falls_back_on_error(self) -> None:
        # 控制台永不因模型挂掉而 500：llm 模式失败也回退规则，只是打上标记。
        transport = FakeTransport(error=RuntimeError("boom"))
        result = classify_auto("上新品", client=LLMClient(SETTINGS, transport=transport))
        self.assertEqual("rules(fallback)", result.engine)

    def test_failure_trips_cooldown(self) -> None:
        transport = FakeTransport(error=RuntimeError("boom"))
        client = LLMClient(SETTINGS, transport=transport)
        first = classify_auto("上新品", client=client)
        self.assertEqual("rules(fallback)", first.engine)
        self.assertEqual(1, len(transport.requests))

        # 冷却期内：直接走规则，一次请求都不发。
        second = classify_auto("上新品", client=client)
        self.assertEqual("rules", second.engine)
        self.assertEqual(1, len(transport.requests))

    def test_success_resets_cooldown(self) -> None:
        failing = FakeTransport(error=RuntimeError("boom"))
        classify_auto("上新品", client=LLMClient(SETTINGS, transport=failing))
        working = FakeTransport(wrap_reply({"intent": "DATA_QUERY", "confidence": 0.9}))
        working_client = LLMClient(SETTINGS, transport=working)

        # 冷却期内不重试。
        classify_auto("查销量", client=working_client)
        self.assertEqual([], working.requests)

        # 冷却解除后恢复调用，成功即清除熔断。
        _reset_cooldown()
        classify_auto("查销量", client=working_client)
        self.assertEqual(1, len(working.requests))
        classify_auto("查销量", client=working_client)
        self.assertEqual(2, len(working.requests))

    def test_engine_mode_invalid_value_maps_to_auto(self) -> None:
        with mock.patch.dict(os.environ, {ENGINE_ENV: "bogus"}):
            self.assertEqual("auto", engine_mode())
        with mock.patch.dict(os.environ, {ENGINE_ENV: "LLM"}):
            self.assertEqual("llm", engine_mode())

    def test_result_serialises_engine(self) -> None:
        transport = FakeTransport(wrap_reply({"intent": "DATA_QUERY", "confidence": 0.9}))
        result = classify_llm("查销量", client=LLMClient(SETTINGS, transport=transport))
        self.assertEqual("llm", result.to_dict()["engine"])


if __name__ == "__main__":
    unittest.main()
