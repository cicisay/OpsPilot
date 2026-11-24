"""OpenAI-compatible chat client. Standard library only, no vendor SDK.

Every provider we support (DeepSeek, Qwen, GLM, Moonshot, OpenAI, Ollama) speaks
the same ``/chat/completions`` shape, so switching model means editing ``.env``,
never editing code.
"""

from __future__ import annotations

import json
import random
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import LLMSettings, llm_settings


class LLMError(Exception):
    """A model call failed. Callers are expected to fall back to the rules."""


class LLMNotConfigured(LLMError):
    """No credentials or endpoint. This is a normal state, not a bug."""


class LLMJsonModeUnsupported(LLMError):
    """Gateway rejected response_format; retry once without it."""


class LLMReasoningBudgetExceeded(LLMError):
    """A reasoning model spent the whole completion budget before writing an answer.

    MiniMax / DeepSeek reasoners emit hidden ``reasoning_content`` tokens that count
    against ``max_tokens``. With a tight cap the visible answer comes back empty and
    ``finish_reason`` is ``length``. The fix is a bigger budget, not a different prompt.
    """


@dataclass
class ToolCall:
    """One function call requested by the model.

    ``arguments`` is the parsed object; ``raw_arguments`` keeps the exact string
    the model produced, which is what must be echoed back in the assistant
    message. Re-serialising a parsed dict can reorder keys and some gateways
    then reject the follow-up turn.
    """

    id: str
    name: str
    arguments: dict
    raw_arguments: str = ""


@dataclass
class LLMReply:
    text: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    # Billed HTTP requests this one logical call took, retries included. A 429
    # storm can push this to 3-6 while ``calls`` still reads 1, which is why the
    # number travels with the reply instead of living only on the client.
    attempts: int = 1

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# transport(url, payload, headers, timeout) -> (status, body_text)
Transport = Callable[[str, dict, dict, float], tuple[int, str]]

# Hard ceiling for the "reasoning ate the budget" retry. Doubling past this is
# a no-op that costs a full second generation and fails identically.
MAX_TOKENS_CEILING = 16384
MAX_TRANSPORT_ATTEMPTS = 3
# Only these are worth paying for twice. Everything else is either the caller's
# fault (4xx) or a permanent deployment problem (501/505).
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter.

    Fixed delays make every client in a fleet retry in lockstep, which keeps
    the rate limit alive. The jitter spreads them out.
    """

    return min(8.0, 1.5 * (2 ** attempt)) * (0.75 + random.random() / 2)


def _urllib_transport(url: str, payload: dict, headers: dict, timeout: float) -> tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", "replace")


_THINK_BLOCK = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN = re.compile(r"^\s*<think(?:ing)?>", re.IGNORECASE)


def strip_think(text: str) -> str:
    """Drop inline ``<think>`` blocks that some models put inside content.

    MiniMax M2.x highspeed and a few Qwen builds emit the chain of thought in the
    visible answer wrapped in tags instead of a separate ``reasoning_content``
    field. Leaving it in place makes the JSON parser grab braces from the
    reasoning. Stripping is lossless for us: we only ever consume the answer.
    """
    cleaned = _THINK_BLOCK.sub("", text or "")
    if _THINK_OPEN.match(cleaned):
        # Unclosed block: everything present is reasoning, no answer yet.
        return ""
    return cleaned.strip()


def _count(value) -> int:
    """Usage counters are integers, but gateways send ``"1,024"`` too."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    try:
        return max(0, int(float(str(value).replace(",", ""))))
    except (TypeError, ValueError):
        return 0


def _estimate_tokens(text: str) -> int:
    """Rough floor for a reply that arrives without a ``usage`` block.

    Four characters per token is the usual English rule of thumb. It is only
    used when the vendor stays silent, and only as a lower bound, so a metered
    gateway is never overridden by the guess.
    """

    return max(1, len(text or "") // 4)


def _prompt_floor(payload: dict) -> int:
    try:
        body = json.dumps(payload.get("messages") or [], ensure_ascii=False)
    except (TypeError, ValueError):
        return 0
    return _estimate_tokens(body)


def _error_message(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return body[:300]
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    if error:
        return str(error)[:300]
    return str(data.get("message") or body)[:300]


def _balanced_object(text: str, start: int) -> str | None:
    """Slice one complete JSON object out of ``text`` starting at ``start``.

    ``rfind("}")`` breaks as soon as the reply continues after the object —
    ``{"a":1} 以上是我的回答`` gave "Extra data", and ``` ```json {...} ``` 希望有帮助``
    did the same. Counting braces (while skipping string literals) finds the
    real end, so trailing prose is ignored instead of failing the whole batch.
    """

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def parse_json_object(text: str) -> dict:
    """Parse a JSON object out of a model reply, tolerating code fences and prose."""
    cleaned = (text or "").strip()
    if not cleaned:
        raise LLMError("empty model reply")
    if cleaned.startswith("```"):
        parts = cleaned.split("\n", 1)
        cleaned = parts[1] if len(parts) > 1 else cleaned
        fence = cleaned.find("```")
        if fence != -1:
            cleaned = cleaned[:fence]
    cleaned = cleaned.strip()

    start = cleaned.find("{")
    if start == -1:
        raise LLMError("reply is not a JSON object")
    candidate = _balanced_object(cleaned, start)
    if candidate is None:
        # Unbalanced braces: fall back to the greedy slice so the error message
        # still reports the real parse failure rather than "not an object".
        end = cleaned.rfind("}")
        if end <= start:
            raise LLMError("reply is not a JSON object")
        candidate = cleaned[start : end + 1]
    try:
        payload = json.loads(candidate)
    except ValueError as exc:
        # One cheap repair before giving up: models write ``{"a": 1,}`` often
        # enough that a trailing comma is a predictable slip, not a broken
        # schema. Only applied after a real failure, so valid JSON is untouched.
        repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
        if repaired == candidate:
            raise LLMError(f"reply is not valid JSON: {exc}") from None
        try:
            payload = json.loads(repaired)
        except ValueError:
            raise LLMError(f"reply is not valid JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise LLMError("reply JSON is not an object")
    return payload


def _parse_tool_calls(raw_calls) -> list[ToolCall]:
    """Read ``message.tool_calls`` without ever raising.

    A malformed argument string is the model's mistake, not a transport error:
    the runtime feeds the empty object back and lets the model try again. An
    exception here would abort a run that is still recoverable.
    """
    calls: list[ToolCall] = []
    for raw in raw_calls or []:
        if not isinstance(raw, dict):
            continue
        function = raw.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        raw_arguments = function.get("arguments")
        if isinstance(raw_arguments, dict):
            arguments = raw_arguments
            raw_text = json.dumps(raw_arguments, ensure_ascii=False)
        else:
            raw_text = str(raw_arguments or "")
            try:
                parsed = json.loads(raw_text) if raw_text.strip() else {}
            except ValueError:
                parsed = {}
            arguments = parsed if isinstance(parsed, dict) else {}
        calls.append(
            ToolCall(
                id=str(raw.get("id") or ""),
                name=name,
                arguments=arguments,
                raw_arguments=raw_text,
            )
        )
    return calls


class LLMClient:
    """Thin wrapper with retries, JSON mode fallback and call counting."""

    def __init__(self, settings: LLMSettings | None = None, transport: Transport | None = None) -> None:
        self.settings = settings or llm_settings()
        self._transport = transport or _urllib_transport
        # ``calls`` is logical model calls; ``attempts`` is what the vendor
        # bills. Keeping both lets the GUI show "6 calls" without hiding that
        # the same 6 calls cost 14 requests.
        self.calls = 0
        self.attempts = 0

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    def chat(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = True,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
    ) -> LLMReply:
        if not self.enabled:
            raise LLMNotConfigured(f"model not configured: {self.settings.describe()}")

        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature if temperature is None else temperature,
            "max_tokens": self.settings.max_tokens if max_tokens is None else max_tokens,
        }
        if tools:
            # json_mode and tools conflict on several compatible gateways; the
            # caller that brings tools gets the native function-calling shape.
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        elif json_mode:
            payload["response_format"] = {"type": "json_object"}

        started_attempts = self.attempts
        try:
            reply = self._post(payload)
        except LLMJsonModeUnsupported:
            # Some self-hosted gateways reject response_format; retry without it.
            payload.pop("response_format", None)
            reply = self._post(payload)
        except LLMReasoningBudgetExceeded:
            # Reasoning ate the budget, or the answer was cut mid-sentence. One
            # retry with headroom, hard-capped so a runaway model still cannot
            # burn an unbounded request.
            doubled = min(max(int(payload["max_tokens"]) * 2, 4096), MAX_TOKENS_CEILING)
            if doubled <= int(payload["max_tokens"]):
                # Already at the ceiling: the same request would be sent again
                # and fail the same way, costing a second full generation.
                raise
            payload["max_tokens"] = doubled
            reply = self._post(payload)
        self.calls += 1
        reply.attempts = max(1, self.attempts - started_attempts)
        return reply

    def chat_json(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[dict, LLMReply]:
        reply = self.chat(messages, temperature=temperature, max_tokens=max_tokens, json_mode=True)
        return parse_json_object(reply.text), reply

    def _post(self, payload: dict) -> LLMReply:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        url = f"{self.settings.base_url}/chat/completions"

        last_error: LLMError | None = None
        for attempt in range(MAX_TRANSPORT_ATTEMPTS):
            # Counted before the request leaves: a timeout may still have been
            # generated and billed upstream, so an attempt is never free.
            self.attempts += 1
            started = time.perf_counter()
            try:
                status, body = self._transport(url, payload, headers, self.settings.timeout)
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
                last_error = LLMError(f"network error: {exc}")
            else:
                if status == 200:
                    return self._parse(body, started, _prompt_floor(payload))
                message = _error_message(body)
                if status == 400 and "response_format" in message.lower():
                    raise LLMJsonModeUnsupported(message)
                error = LLMError(f"http {status}: {message}")
                # Retrying a 501/505 only multiplies the bill; those are
                # permanent until someone changes the deployment.
                if status not in RETRYABLE_STATUS:
                    raise error
                last_error = error
            if attempt < MAX_TRANSPORT_ATTEMPTS - 1:
                time.sleep(_backoff(attempt))
        raise last_error or LLMError("unknown model error")

    def _parse(self, body: str, started: float, prompt_floor: int = 0) -> LLMReply:
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise LLMError(f"invalid response body: {exc}") from None
        if not isinstance(data, dict):
            raise LLMError("invalid response body: not an object")
        choices = data.get("choices") or []
        if not isinstance(choices, list) or not choices:
            raise LLMError("response has no choices")
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        text = strip_think(str(message.get("content") or ""))
        finish_reason = str(choice.get("finish_reason") or "")
        if finish_reason == "length":
            # Truncation is not only "empty answer": a reply cut mid-string is
            # worse, because it survives whitespace checks and then fails JSON
            # parsing with a misleading error. Treat both as a budget problem.
            raise LLMReasoningBudgetExceeded(
                f"completion budget exhausted (finish_reason=length, {len(text)} visible chars)"
            )
        return LLMReply(
            text=text,
            model=str(data.get("model") or self.settings.model),
            # A gateway that omits ``usage`` must not silently reset the token
            # budget to zero; estimate a floor instead of trusting the absence.
            prompt_tokens=_count(usage.get("prompt_tokens")) or prompt_floor,
            completion_tokens=_count(usage.get("completion_tokens")) or _estimate_tokens(text),
            latency_ms=int((time.perf_counter() - started) * 1000),
            raw=data,
            tool_calls=_parse_tool_calls(message.get("tool_calls")),
            finish_reason=finish_reason,
        )
