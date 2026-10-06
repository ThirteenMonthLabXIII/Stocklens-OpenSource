from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal


ActionKind = Literal["execute", "navigate", "info", "external"]
WorkflowStatus = Literal["success", "partial", "failed", "skipped"]


@dataclass(frozen=True)
class WorkflowResult:
    status: WorkflowStatus
    message: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # Non-serializable worker outputs stay out of the user-facing artifact
    # contract but can be transferred back to the Qt thread safely.
    private_artifacts: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class ActionDescriptor:
    id: str
    label: str
    surface: str
    kind: ActionKind
    risk_level: str = "low"
    preconditions: tuple[str, ...] = ()
    run: Callable[[], Any] | None = None
    status_message: str = ""

