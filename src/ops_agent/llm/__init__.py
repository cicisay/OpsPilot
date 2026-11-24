"""LLM layer: OpenAI-compatible client, JSON helpers and call tracing."""

from .client import (
    LLMClient,
    LLMError,
    LLMJsonModeUnsupported,
    LLMNotConfigured,
    LLMReply,
    ToolCall,
    parse_json_object,
)
from .trace import TraceRecorder

__all__ = [
    "LLMClient",
    "LLMError",
    "LLMJsonModeUnsupported",
    "LLMNotConfigured",
    "LLMReply",
    "ToolCall",
    "TraceRecorder",
    "parse_json_object",
]
