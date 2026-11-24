"""Project paths and runtime settings shared by the CLI, agents and tests.

Secrets never live in code. Model credentials are read from the project ``.env``
file or the process environment, and the environment always wins, so a single
deployment can switch providers without touching a line of source.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
ENV_FILE = PROJECT_ROOT / ".env"
TRACE_DIR = DATA_DIR / "llm_trace"

# provider -> (default base_url, default model)
PROVIDER_PRESETS: dict[str, tuple[str, str]] = {
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "glm": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-plus"),
    "moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "minimax": ("https://api.minimaxi.com/v1", "MiniMax-M2.7-highspeed"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "ollama": ("http://127.0.0.1:11434/v1", "qwen2.5:14b"),
}

# Providers that run locally and therefore do not need a key.
LOCAL_PROVIDERS = frozenset({"ollama", "local"})

# Providers whose OpenAI-compatible endpoint accepts ``tools`` / ``tool_calls``.
# An allowlist, not a denylist: a gateway that silently ignores ``tools`` would
# turn the agent loop into a model that answers in prose forever.
TOOL_CAPABLE_PROVIDERS = frozenset(
    {"deepseek", "qwen", "glm", "moonshot", "minimax", "openai"}
)


DEFAULT_TENANT_SLUG = "globaltrade"


def default_data_dir() -> Path:
    """Where one tenant's run artefacts live.

    The console reads ``data/tenants/<slug>/*.json``; the CLI used to write the
    legacy root copies, so fresh runs never appeared in the UI. Both now agree.
    Order: explicit env override -> tenant directory -> legacy root.
    """
    load_env()
    override = (os.environ.get("OPS_TENANT_DATA_DIR") or "").strip()
    if override:
        return Path(override)
    tenant = DATA_DIR / "tenants" / DEFAULT_TENANT_SLUG
    return tenant if tenant.is_dir() else DATA_DIR


def default_snapshot_path() -> Path:
    """The site snapshot every entry point reads.

    A stale root copy is how a 50-product site kept showing 30 rows in offline
    runs. Order: explicit env override -> tenant snapshot -> legacy root.
    """
    load_env()
    override = (os.environ.get("OPS_SNAPSHOT_PATH") or "").strip()
    if override:
        return Path(override)
    tenant = default_data_dir() / "site_snapshot.json"
    return tenant if tenant.exists() else DATA_DIR / "site_snapshot.json"


def default_lexicon_path() -> Path:
    """The compliance word list, following the same tenant -> root order.

    Every caller (agents, CLI, console) has to resolve it the same way, otherwise
    an operator edits the file and one code path keeps enforcing a built-in copy.
    """
    load_env()
    override = (os.environ.get("OPS_LEXICON_PATH") or "").strip()
    if override:
        return Path(override)
    tenant = default_data_dir() / "compliance" / "lexicon.json"
    return tenant if tenant.exists() else DATA_DIR / "compliance" / "lexicon.json"


def load_env(path: Path | str | None = None) -> None:
    """Load a minimal .env file without pulling in a dependency.

    Existing environment variables are never overwritten, so CI secrets and
    shell exports keep priority over the file.
    """
    target = Path(path) if path else ENV_FILE
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class LLMSettings:
    """Everything needed to talk to one OpenAI-compatible endpoint."""

    provider: str
    base_url: str
    model: str
    api_key: str
    timeout: float = 120.0
    max_tokens: int = 4096
    temperature: float = 0.4
    max_calls_per_item: int = 6
    max_revision_rounds: int = 2

    @property
    def needs_key(self) -> bool:
        return self.provider not in LOCAL_PROVIDERS

    @property
    def enabled(self) -> bool:
        """Configured enough to make a call? No key means rule-only mode."""
        if not self.base_url or not self.model:
            return False
        return bool(self.api_key) if self.needs_key else True

    def describe(self) -> str:
        return f"{self.provider}/{self.model} ({'on' if self.enabled else 'off, rule fallback'})"


def llm_settings() -> LLMSettings:
    load_env()
    provider = (os.environ.get("LLM_PROVIDER") or "deepseek").strip().lower()
    default_base, default_model = PROVIDER_PRESETS.get(provider, ("", ""))
    return LLMSettings(
        provider=provider,
        base_url=(os.environ.get("LLM_BASE_URL") or default_base).strip().rstrip("/"),
        model=(os.environ.get("LLM_MODEL") or default_model).strip(),
        api_key=(os.environ.get("LLM_API_KEY") or "").strip(),
        timeout=_float("LLM_TIMEOUT", 120.0),
        max_tokens=_int("LLM_MAX_TOKENS", 4096),
        temperature=_float("LLM_TEMPERATURE", 0.4),
        max_calls_per_item=_int("LLM_MAX_CALLS_PER_ITEM", 6),
        max_revision_rounds=_int("LLM_MAX_REVISION_ROUNDS", 2),
    )


def provider_supports_tools(provider: str) -> bool:
    """Can this endpoint do native function calling?

    ``ollama`` runs locally and usually can, but it is opt-in: a stock Ollama
    build answers a ``tools`` request with prose, which would look like a model
    that refuses to use tools rather than a configuration mistake.
    """
    load_env()
    name = str(provider or "").strip().lower()
    if name in TOOL_CAPABLE_PROVIDERS:
        return True
    if name in LOCAL_PROVIDERS:
        return (os.environ.get("LLM_TOOLS") or "").strip().lower() in ("1", "true", "yes", "on")
    return False


def _int(name: str, fallback: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError:
        return fallback


def _float(name: str, fallback: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return fallback
    try:
        return float(raw)
    except ValueError:
        return fallback
