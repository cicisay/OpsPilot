import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.lessons import (  # noqa: E402
    LessonError,
    derive_lessons,
    lesson_id,
    normalize_lessons,
)
from ops_agent.llm.client import LLMError, LLMReply  # noqa: E402


def experiment(**overrides) -> dict:
    base = {
        "product_id": "p1",
        "category": "home-kitchen",
        "angle": "把盖型差异写进标题",
        "hypothesis": "把盖型写进标题可减少错发",
        "success_metric": "order_rate_pct从当前0.14%提升至至少0.18%",
        "verdict": "supported",
        "verdict_cn": "成立",
        "outcome": {
            "lift": 0.25,
            "min_lift": 0.25,
            "p_value": 0.012,
            "baseline": {"order_rate": 0.0014, "click_rate": 0.05, "return_rate": 0.0},
            "treated": {"order_rate": 0.0018, "click_rate": 0.06, "return_rate": 0.0},
            "note": "提升 25.0% ≥ 目标 25.0%",
        },
    }
    base.update(overrides)
    return base


class NormalizeTests(unittest.TestCase):
    def test_grounded_percentage_survives(self):
        payload = {
            "lessons": [
                {
                    "scope": "home-kitchen",
                    "statement": "把盖型写进标题，订单率提升 25%",
                    "direction": "worked",
                    "evidence_count": 2,
                    "confidence": 0.6,
                }
            ]
        }
        lessons = normalize_lessons(payload, [experiment()])
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]["confidence"], 0.6)
        self.assertTrue(lessons[0]["lesson_id"].startswith("L"))

    def test_invented_percentage_is_dropped(self):
        payload = {
            "lessons": [
                {
                    "scope": "home-kitchen",
                    "statement": "把盖型写进标题，订单率提升 40%",
                    "direction": "worked",
                    "evidence_count": 2,
                    "confidence": 0.7,
                }
            ]
        }
        issues = []
        lessons = normalize_lessons(payload, [experiment()], issues)
        self.assertEqual(lessons, [])
        self.assertTrue(any("ungrounded" in item for item in issues))

    def test_statement_without_numbers_is_allowed(self):
        payload = {"lessons": [{"scope": "store", "statement": "认证信息写进卖点更稳"}]}
        lessons = normalize_lessons(payload, [experiment()])
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]["direction"], "mixed")

    def test_single_experiment_caps_confidence(self):
        payload = {
            "lessons": [
                {
                    "scope": "store",
                    "statement": "认证信息写进卖点更稳",
                    "evidence_count": 1,
                    "confidence": 0.95,
                }
            ]
        }
        lessons = normalize_lessons(payload, [experiment()])
        self.assertEqual(lessons[0]["confidence"], 0.4)

    def test_duplicates_collapse(self):
        payload = {
            "lessons": [
                {"scope": "store", "statement": "认证信息写进卖点更稳", "evidence_count": 2},
                {"scope": "Store", "statement": "认证信息写进卖点更稳", "evidence_count": 3},
            ]
        }
        self.assertEqual(len(normalize_lessons(payload, [experiment()])), 1)

    def test_missing_fields_are_dropped(self):
        payload = {"lessons": [{"scope": "", "statement": "x"}, {"scope": "store", "statement": ""}]}
        self.assertEqual(normalize_lessons(payload, [experiment()]), [])

    def test_bad_payload_shape_returns_empty(self):
        issues = []
        self.assertEqual(normalize_lessons({"nope": []}, [experiment()], issues), [])
        self.assertTrue(issues)

    def test_long_statement_is_truncated(self):
        payload = {"lessons": [{"scope": "store", "statement": "长" * 500, "evidence_count": 2}]}
        lessons = normalize_lessons(payload, [experiment()])
        self.assertEqual(len(lessons[0]["statement"]), 200)

    def test_negative_lift_grounds_a_drop_statement(self):
        rec = experiment(verdict="rejected")
        rec["outcome"]["lift"] = -0.12
        payload = {
            "lessons": [
                {"scope": "store", "statement": "降价后订单率下降 12%", "evidence_count": 2, "confidence": 0.5}
            ]
        }
        self.assertEqual(len(normalize_lessons(payload, [rec])), 1)

    def test_percentage_from_the_angle_is_allowed(self):
        rec = experiment(angle="单纯降价 8% 不改文案")
        payload = {
            "lessons": [
                {"scope": "store", "statement": "单纯降价 8% 没有效果", "evidence_count": 2, "confidence": 0.5}
            ]
        }
        self.assertEqual(len(normalize_lessons(payload, [rec])), 1)

    def test_lesson_id_is_stable(self):
        self.assertEqual(lesson_id("a", "b"), lesson_id("a", "b"))
        self.assertNotEqual(lesson_id("a", "b"), lesson_id("a", "c"))


class FakeReply:
    def __init__(self, payload, model="test-model"):
        self.text = json.dumps(payload, ensure_ascii=False)
        self.model = model
        self.prompt_tokens = 10
        self.completion_tokens = 5
        self.latency_ms = 20


class FakeClient:
    def __init__(self, replies):
        from ops_agent.llm.client import LLMSettings

        self.settings = LLMSettings(
            provider="test", base_url="http://x", api_key="k", model="test-model"
        )
        self._replies = list(replies)
        self.calls = 0

    @property
    def enabled(self):
        return True

    def chat(self, messages, **kwargs):
        self.calls += 1
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class DeriveTests(unittest.TestCase):
    def test_returns_lessons_and_model(self):
        client = FakeClient(
            [FakeReply({"lessons": [{"scope": "home-kitchen", "statement": "盖型写进标题",
                                     "direction": "worked", "evidence_count": 2, "confidence": 0.5}]})]
        )
        lessons, model, issues = derive_lessons(client, [experiment()], [])
        self.assertEqual(len(lessons), 1)
        self.assertEqual(model, "test-model")
        self.assertEqual(issues, [])

    def test_no_model_raises(self):
        class Off:
            enabled = False

            class settings:
                @staticmethod
                def describe():
                    return "off"

        with self.assertRaises(LessonError):
            derive_lessons(Off(), [experiment()], [])

    def test_no_records_raises(self):
        with self.assertRaises(LessonError):
            derive_lessons(FakeClient([]), [], [])

    def test_broken_json_raises(self):
        client = FakeClient([LLMError("reply is not valid JSON")])
        with self.assertRaises(LessonError):
            derive_lessons(client, [experiment()], [])

    def test_empty_reply_keeps_old_lessons(self):
        client = FakeClient([FakeReply({"lessons": []})])
        lessons, _model, issues = derive_lessons(
            client, [experiment()], [{"scope": "store", "statement": "旧经验", "confidence": 0.5}]
        )
        self.assertEqual(lessons, [])
        self.assertTrue(any("保留原经验库" in item for item in issues))


if __name__ == "__main__":
    unittest.main()
