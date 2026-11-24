import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ops_agent.config import LLMSettings, load_env, llm_settings  # noqa: E402
from ops_agent.llm import (  # noqa: E402
    LLMClient,
    LLMError,
    LLMNotConfigured,
    parse_json_object,
)
from ops_agent.llm.client import strip_think  # noqa: E402
from ops_agent.llm.trace import TraceRecorder  # noqa: E402

SETTINGS = LLMSettings(
    provider="deepseek",
    base_url="https://api.deepseek.com/v1",
    model="deepseek-chat",
    api_key="test-key",
)


def body(content: str, prompt: int = 11, completion: int = 7, finish: str = "stop") -> str:
    return json.dumps(
        {
            "model": "deepseek-chat",
            "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
        }
    )


class JsonParsingTests(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(parse_json_object('{"a": 1}'), {"a": 1})

    def test_code_fence(self):
        self.assertEqual(parse_json_object('```json\n{"a": 2}\n```'), {"a": 2})

    def test_surrounded_by_prose(self):
        self.assertEqual(parse_json_object('好的：{"a": 3} 完成'), {"a": 3})

    def test_object_followed_by_a_sentence(self):
        """`{"a":1} 以上是我的回答` used to raise Extra data and abort a batch."""
        self.assertEqual(parse_json_object('{"a": 1} 以上是我的回答。'), {"a": 1})

    def test_code_fence_with_trailing_prose(self):
        self.assertEqual(
            parse_json_object('```json\n{"a": 1}\n```\n希望有帮助'), {"a": 1}
        )

    def test_brace_inside_a_string_does_not_end_the_object(self):
        self.assertEqual(
            parse_json_object('{"a": {"b": "}"}, "c": 2} 后面的说明'),
            {"a": {"b": "}"}, "c": 2},
        )

    def test_trailing_comma_is_repaired(self):
        self.assertEqual(parse_json_object('{"a": 1,}'), {"a": 1})
        self.assertEqual(parse_json_object('{"a": [1, 2,],}'), {"a": [1, 2]})

    def test_invalid_json_raises(self):
        with self.assertRaises(LLMError):
            parse_json_object("not json at all")

    def test_empty_raises(self):
        with self.assertRaises(LLMError):
            parse_json_object("")


class ClientTests(unittest.TestCase):
    def test_unconfigured_raises(self):
        client = LLMClient(LLMSettings(provider="deepseek", base_url="", model="", api_key=""))
        self.assertFalse(client.enabled)
        with self.assertRaises(LLMNotConfigured):
            client.chat([{"role": "user", "content": "hi"}])

    def test_successful_call_records_tokens(self):
        calls = []

        def transport(url, payload, headers, timeout):
            calls.append(url)
            return 200, body('{"ok": true}')

        client = LLMClient(SETTINGS, transport=transport)
        reply = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(reply.text, '{"ok": true}')
        self.assertEqual(reply.total_tokens, 18)
        self.assertEqual(client.calls, 1)
        self.assertEqual(calls[0], "https://api.deepseek.com/v1/chat/completions")

    def test_server_error_is_retried(self):
        attempts = {"n": 0}

        def transport(url, payload, headers, timeout):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return 500, '{"error": {"message": "boom"}}'
            return 200, body('{"ok": true}')

        client = LLMClient(SETTINGS, transport=transport)
        with patch("ops_agent.llm.client.time.sleep"):
            reply = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(reply.text, '{"ok": true}')
        self.assertEqual(attempts["n"], 2)

    def test_auth_error_is_not_retried(self):
        attempts = {"n": 0}

        def transport(url, payload, headers, timeout):
            attempts["n"] += 1
            return 401, '{"error": {"message": "bad key"}}'

        client = LLMClient(SETTINGS, transport=transport)
        with self.assertRaises(LLMError) as ctx:
            client.chat([{"role": "user", "content": "hi"}])
        self.assertIn("401", str(ctx.exception))
        self.assertEqual(attempts["n"], 1)

    def test_json_mode_falls_back_when_rejected(self):
        seen = []

        def transport(url, payload, headers, timeout):
            seen.append("response_format" in payload)
            if len(seen) == 1:
                return 400, '{"error": {"message": "response_format unsupported"}}'
            return 200, body('{"ok": true}')

        client = LLMClient(SETTINGS, transport=transport)
        reply = client.chat([{"role": "user", "content": "hi"}], json_mode=True)
        self.assertEqual(reply.text, '{"ok": true}')
        self.assertEqual(seen, [True, False])

    def test_chat_json_parses_content(self):
        def transport(url, payload, headers, timeout):
            return 200, body('{"variants": [1, 2]}')

        client = LLMClient(SETTINGS, transport=transport)
        payload, reply = client.chat_json([{"role": "user", "content": "hi"}])
        self.assertEqual(payload, {"variants": [1, 2]})
        self.assertEqual(reply.prompt_tokens, 11)

    def test_inline_think_block_is_ignored(self):
        """MiniMax highspeed models wrap chain-of-thought in <think> inside content."""
        def transport(url, payload, headers, timeout):
            return 200, body('<think>compare {"fake": 0} then answer</think>{"ok": true}')

        client = LLMClient(SETTINGS, transport=transport)
        payload, reply = client.chat_json([{"role": "user", "content": "hi"}])
        self.assertEqual(payload, {"ok": True})
        self.assertNotIn("<think>", reply.text)

    def test_unclosed_think_block_yields_no_answer(self):
        self.assertEqual(strip_think("<think> still thinking"), "")

    def test_reasoning_budget_exhausted_retries_with_headroom(self):
        """A reasoning model can spend max_tokens before writing any content."""
        seen = []

        def transport(url, payload, headers, timeout):
            seen.append(payload["max_tokens"])
            if len(seen) == 1:
                return 200, body("", finish="length")
            return 200, body('{"ok": true}')

        client = LLMClient(SETTINGS, transport=transport)
        payload, _ = client.chat_json([{"role": "user", "content": "hi"}], max_tokens=200)
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(seen, [200, 4096])
        self.assertEqual(client.calls, 1)

    def test_truncated_non_empty_reply_retries_instead_of_failing_json(self):
        """A half-written JSON object is a budget problem, not a parse problem."""
        seen = []

        def transport(url, payload, headers, timeout):
            seen.append(payload["max_tokens"])
            if len(seen) == 1:
                return 200, body('{"skus": [{"problem": "cut', finish="length")
            return 200, body('{"skus": []}')

        client = LLMClient(SETTINGS, transport=transport)
        payload, _ = client.chat_json([{"role": "user", "content": "hi"}], max_tokens=2048)
        self.assertEqual(payload, {"skus": []})
        self.assertEqual(seen, [2048, 4096])
        self.assertEqual(client.calls, 1)

    def test_retries_at_the_token_ceiling_are_not_resent(self):
        """Doubling 16384 is still 16384; resending just pays for the same cut."""
        seen = []

        def transport(url, payload, headers, timeout):
            seen.append(payload["max_tokens"])
            return 200, body("", finish="length")

        client = LLMClient(SETTINGS, transport=transport)
        with self.assertRaises(LLMError):
            client.chat([{"role": "user", "content": "hi"}], max_tokens=16384)
        self.assertEqual(seen, [16384])

    def test_retry_count_travels_with_the_reply(self):
        """`calls` counts logical calls; `attempts` is what the vendor bills."""

        def transport(url, payload, headers, timeout):
            if client.attempts < 3:
                return 500, '{"error": {"message": "boom"}}'
            return 200, body('{"ok": true}')

        client = LLMClient(SETTINGS, transport=transport)
        with patch("ops_agent.llm.client.time.sleep"):
            reply = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(reply.text, '{"ok": true}')
        self.assertEqual(client.calls, 1)
        self.assertEqual(client.attempts, 3)
        self.assertEqual(reply.attempts, 3)

    def test_permanent_server_error_is_not_retried(self):
        """501/505 will not fix themselves; three tries only triple the bill."""
        seen = {"n": 0}

        def transport(url, payload, headers, timeout):
            seen["n"] += 1
            return 501, '{"error": {"message": "not implemented"}}'

        client = LLMClient(SETTINGS, transport=transport)
        with self.assertRaises(LLMError):
            client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(seen["n"], 1)
        self.assertEqual(client.attempts, 1)

    def test_usage_with_thousands_separator_does_not_crash(self):
        def transport(url, payload, headers, timeout):
            return 200, json.dumps(
                {
                    "model": "deepseek-chat",
                    "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": "1,024", "completion_tokens": "12"},
                }
            )

        client = LLMClient(SETTINGS, transport=transport)
        reply = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(reply.prompt_tokens, 1024)
        self.assertEqual(reply.completion_tokens, 12)

    def test_missing_usage_still_reports_tokens(self):
        """A gateway that omits `usage` must not reset the token budget to zero."""

        def transport(url, payload, headers, timeout):
            return 200, json.dumps(
                {
                    "model": "deepseek-chat",
                    "choices": [
                        {"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}
                    ],
                }
            )

        client = LLMClient(SETTINGS, transport=transport)
        reply = client.chat([{"role": "user", "content": "hi"}])
        self.assertGreater(reply.prompt_tokens, 0)
        self.assertGreater(reply.completion_tokens, 0)


class SettingsTests(unittest.TestCase):
    def test_env_file_is_loaded_without_overriding(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("OPS_TEST_KEY=from-file\nOPS_TEST_KEEP=file\n", encoding="utf-8")
            os.environ["OPS_TEST_KEEP"] = "shell"
            try:
                load_env(env_path)
                self.assertEqual(os.environ.get("OPS_TEST_KEY"), "from-file")
                self.assertEqual(os.environ.get("OPS_TEST_KEEP"), "shell")
            finally:
                os.environ.pop("OPS_TEST_KEY", None)
                os.environ.pop("OPS_TEST_KEEP", None)

    def test_provider_defaults_apply(self):
        env = {"LLM_PROVIDER": "qwen", "LLM_API_KEY": "k", "LLM_MODEL": "", "LLM_BASE_URL": ""}
        with patch.dict(os.environ, env, clear=False):
            settings = llm_settings()
        self.assertEqual(settings.model, "qwen-plus")
        self.assertIn("dashscope", settings.base_url)
        self.assertTrue(settings.enabled)

    def test_local_provider_needs_no_key(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "LLM_API_KEY": ""}, clear=False):
            settings = llm_settings()
        self.assertTrue(settings.enabled)


class TraceTests(unittest.TestCase):
    def test_records_and_summarises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.jsonl"
            recorder = TraceRecorder(run_id="t1", path=path)
            recorder.record(
                agent="copywriter",
                model="deepseek-chat",
                messages=[{"role": "user", "content": "hello"}],
                response="world",
                prompt_tokens=5,
                completion_tokens=3,
            )
            recorder.record(agent="critic", ok=False, error="timeout")
            summary = recorder.summary()
            self.assertEqual(summary["calls"], 2)
            self.assertEqual(summary["failures"], 1)
            self.assertEqual(summary["tokens"], 8)
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["agent"], "copywriter")


if __name__ == "__main__":
    unittest.main()
