"""Tool gateway: the only door between a model and a tool.

Three jobs, all of them code-side:

* **Visibility.** A role sees a filtered tool list. An agent that may only read
  never sees ``publish_listing``, so it cannot ask for it by accident or by
  persuasion.
* **Containment.** Asking for a hidden tool returns a neutral observation. The
  reply never describes the tool or names its siblings, so a probing model
  learns "no" without learning what else exists.
* **Provenance.** Every executed call gets an observation id and, optionally,
  an audit line. The model cites ``obs-4``; the ledger knows what that was.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..agents.base import FORBIDDEN_TOOLS
from .risk import check_level, infer_risk_level, is_visible

# Retained for callers that want the OpenAI shape without importing gateway.
_SCHEMA_FALLBACK = {"type": "object", "properties": {}, "additionalProperties": False}


@dataclass(frozen=True)
class AgentPolicy:
    """What one role is allowed to do, declared once and enforced in one place."""

    role: str
    # Read-only by default. Write tools need an explicit, reviewed widening.
    side_effects: frozenset = frozenset({"read"})
    # Empty means "no name-level restriction"; side_effects still applies.
    tools: frozenset = frozenset()
    # 风险上限 R0—R3。None = 不做等级限制（改造前的行为，一个字节都不变）；
    # R3 无论如何都不可见，与这里取什么值无关。policy 是 frozen 的，改上限必须
    # 新建 AgentPolicy + 新建 ToolGateway——_visible_specs() 的缓存刻意不失效。
    max_risk: str | None = None
    # No budget here on purpose: Runtime injects Budget.max_tool_calls, so the
    # two numbers cannot drift apart.

    def __post_init__(self) -> None:
        if self.max_risk is not None:
            # 写错等级（"R4"、""）在这里就炸，而不是让过滤静默放行一切。
            object.__setattr__(self, "max_risk", check_level(self.max_risk))


class ToolGateway:
    def __init__(
        self,
        registry,
        policy: AgentPolicy,
        ledger,
        audit: Callable[[dict], None] | None = None,
        max_tool_calls: int | None = None,
    ) -> None:
        # Construction-time mistakes are bugs: fail before a model can see them.
        if registry is None:
            raise ValueError("registry 不能为空")
        if policy is None:
            raise ValueError("policy 不能为空")
        if not policy.side_effects:
            raise ValueError("policy.side_effects 不能为空：没有允许的副作用类型")
        self.registry = registry
        self.policy = policy
        self.ledger = ledger
        self.audit = audit
        self.max_tool_calls = max_tool_calls
        # Two counters, two meanings. attempts is what the budget caps; executed
        # is what actually reached the registry. A model that keeps guessing
        # names burns attempts without burning your data.
        self._attempts = 0
        self._executed = 0
        self._visible: dict[str, dict] | None = None
        self._known: set[str] | None = None
        if not self._visible_specs():
            raise ValueError("policy 没有暴露任何工具：检查 side_effects 与 tools 过滤条件")

    # ------------------------------------------------------------- visibility

    def _visible_specs(self) -> dict[str, dict]:
        """name -> raw ToolSpec dict, filtered once and cached."""
        if self._visible is not None:
            return self._visible
        visible: dict[str, dict] = {}
        for spec in self.registry.specs():
            name = str(spec.get("name") or "")
            if not name:
                continue
            if self.policy.tools and name not in self.policy.tools:
                continue
            if str(spec.get("side_effect") or "") not in self.policy.side_effects:
                continue
            # Belt and braces: the forbidden list wins even if a future policy
            # is widened by mistake.
            if name in FORBIDDEN_TOOLS:
                continue
            # spec 可能来自手工造的 registry（测试里的 FakeRegistry），没有
            # risk_level 就按规则现推，不能让"缺字段"变成"默认放行"。
            level = str(spec.get("risk_level") or "") or infer_risk_level(
                name,
                str(spec.get("side_effect") or ""),
                bool(spec.get("requires_approval")),
            )
            if not is_visible(level, self.policy.max_risk):
                continue
            visible[name] = spec
        self._visible = visible
        return visible

    def specs(self) -> list[dict]:
        """OpenAI function-calling schema; only tools the policy allows."""
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(spec.get("description") or ""),
                    "parameters": spec.get("input_schema") or dict(_SCHEMA_FALLBACK),
                },
            }
            for name, spec in self._visible_specs().items()
        ]

    def allowed_names(self) -> list[str]:
        return list(self._visible_specs())

    def _known_names(self) -> set[str]:
        """Every name in the registry, including the ones this role cannot use."""
        if self._known is None:
            self._known = {str(spec.get("name") or "") for spec in self.registry.specs()}
        return self._known

    # ------------------------------------------------------------------- call

    def set_budget(self, max_tool_calls: int | None) -> None:
        """Runtime injects Budget.max_tool_calls here; there is no second copy."""
        self.max_tool_calls = max_tool_calls

    def reset_counters(self) -> None:
        """A Budget is per run, so the accounting restarts with each run()."""
        self._attempts = 0
        self._executed = 0

    @staticmethod
    def _error(code: str, message: str) -> dict:
        return {"obs": "", "ok": False, "code": code, "data": None, "error": message}

    def _audit_event(self, event: dict) -> None:
        if self.audit is not None:
            self.audit(event)

    def call(self, name: str, arguments: dict, ctx) -> dict:
        """{"obs": "obs-3", "ok": bool, "code": str|None, "data": dict|None, "error": str|None}

        Every model mistake is an observation it can recover from. Only a
        construction-time misconfiguration raises, and that happens in __init__.
        """
        # Counted on entry, before anything can fail: a model that keeps
        # guessing tool names must burn budget just like a productive one.
        self._attempts += 1
        if self.max_tool_calls is not None and self._attempts > self.max_tool_calls:
            return self._error(
                "tool_call_budget_exceeded",
                "tool budget exhausted; finish with the terminal tool",
            )

        if name not in self._visible_specs():
            if name in self._known_names():
                # Security signal: a real capability was requested out of scope.
                # The message must not describe the tool or name its siblings.
                self._audit_event(
                    {
                        "kind": "permission_denied",
                        "level": "warning",
                        "role": self.policy.role,
                        "tool": name,
                        "attempt": self._attempts,
                    }
                )
                return self._error(
                    "tool_not_permitted",
                    "tool not available to this role; choose from the exposed tools",
                )
            # Quality signal: the model invented a name. Nothing to protect here.
            self._audit_event(
                {
                    "kind": "unknown_tool",
                    "level": "info",
                    "role": self.policy.role,
                    "tool": name,
                    "attempt": self._attempts,
                }
            )
            return self._error("unknown_tool", f"unknown tool: {name}")

        self._executed += 1
        result = self.registry.call(name, arguments or {}, ctx)
        payload = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        obs_id = self.ledger.add(name, arguments or {}, payload)
        # Return what the ledger kept, not the raw payload: a shaped result is
        # what the model should see, and it is what a citation will resolve to.
        effective = self.ledger.result(obs_id) or payload
        out = {
            "obs": obs_id,
            "ok": bool(effective.get("ok")),
            "code": effective.get("code"),
            "data": effective.get("data"),
            "error": effective.get("error"),
        }
        self._audit_event(
            {
                "kind": "tool_call",
                "level": "info",
                "role": self.policy.role,
                "tool": name,
                "obs": obs_id,
                "ok": out["ok"],
                "code": out["code"],
                "error": out["error"],
                "attempt": self._attempts,
            }
        )
        return out

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def executed(self) -> int:
        return self._executed

    @property
    def call_count(self) -> int:
        """Deprecated alias for attempts; kept so older callers keep working."""
        return self._attempts


__all__ = ["AgentPolicy", "ToolGateway"]
