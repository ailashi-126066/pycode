# Pi 没有，pycode 新增
"""Plan identity and approved document data."""

import re
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class PlanState:
    plan_id: str
    virtual_path: str
    storage_path: str
    session_id: str
    original_mode: str = "default"
    mode: Literal["planning", "auto", "review", "completed"] = "planning"
    approved_run_id: str | None = None
    document_exists: bool = False

    def __post_init__(self):
        if (
            not re.fullmatch(r"[a-f0-9]{32}", self.plan_id)
            or self.virtual_path != "/plans/" + self.plan_id + ".md"
        ):
            raise ValueError("Plan identity and virtual path do not agree")
        if self.mode not in {"planning", "auto", "review", "completed"} or not self.session_id:
            raise ValueError("Plan mode and owner must be valid")


@dataclass(frozen=True)
class PlanDocument:
    content: str
    sha256: str
    storage_path: str
