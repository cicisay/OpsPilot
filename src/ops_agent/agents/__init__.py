"""Multi-agent layer: a coordinator plus specialists, with hard boundaries."""

from .base import (
    ALLOWED_TOOL_SIDE_EFFECTS,
    EDITABLE_PRODUCT_FIELDS,
    FORBIDDEN_FIELDS,
    FORBIDDEN_TOOLS,
    GuardResult,
    RedLineGuard,
    WorkContext,
    allowed_tools,
)
from .copywriter import CopywriterAgent
from .compliance_agent import (
    ComplianceAgent,
    ComplianceError,
    ComplianceResult,
    run_compliance,
)
from .critic import CriticAgent
from .director import DirectorAgent, DirectorResult, run_director
from .orchestrator import CopyOrchestrator, CopyOutcome
from .strategist import StrategistAgent
from .workshop_agent import WorkshopAgent, WorkshopError, WorkshopResult, run_workshop_task

__all__ = [
    "ALLOWED_TOOL_SIDE_EFFECTS",
    "ComplianceAgent",
    "ComplianceError",
    "ComplianceResult",
    "CopyOrchestrator",
    "CopyOutcome",
    "CopywriterAgent",
    "CriticAgent",
    "DirectorAgent",
    "DirectorResult",
    "EDITABLE_PRODUCT_FIELDS",
    "FORBIDDEN_FIELDS",
    "FORBIDDEN_TOOLS",
    "GuardResult",
    "RedLineGuard",
    "StrategistAgent",
    "WorkContext",
    "WorkshopAgent",
    "WorkshopError",
    "WorkshopResult",
    "allowed_tools",
    "run_compliance",
    "run_director",
    "run_workshop_task",
]
