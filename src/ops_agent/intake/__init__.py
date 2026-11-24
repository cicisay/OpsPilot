"""意图层（Intake）：把一句自然语言委托变成「意图 + 槽位 + 产出物 + 工具集 + 风险上限」。

两个引擎：
- 规则版 ``classify``：确定性、零成本，评测与 CI 用它；
- 大模型版 ``classify_llm`` / ``classify_auto``：活路径（控制台 / CLI）用，
  失败自动回退规则并带熔断，引擎选择由 ``INTAKE_ENGINE`` 控制（auto/llm/rules）。
"""

from .llm_router import ENGINE_ENV, classify_auto, classify_llm, engine_mode
from .router import classify, explain, normalize
from .schema import INTENT_SPECS, PLANNED_TOOLS, RISK_LEVELS, Intent, IntentResult

__all__ = [
    "ENGINE_ENV",
    "INTENT_SPECS",
    "PLANNED_TOOLS",
    "RISK_LEVELS",
    "Intent",
    "IntentResult",
    "classify",
    "classify_auto",
    "classify_llm",
    "engine_mode",
    "explain",
    "normalize",
]
