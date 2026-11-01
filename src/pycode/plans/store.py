# Pi 没有，pycode 新增
"""The active Plan document, guarded by ScopedBackend."""

import hashlib
import re
import stat
from pathlib import Path

from pycode.kernel import ToolError, owned_io
from pycode.plans.types import PlanDocument
from pycode.tools import ScopedBackend


class FilePlanStore:
    def __init__(self, root: Path, *, backend_factory):
        self.root = root.resolve()
        self.active_name = None
        inner = backend_factory(
            self.root,
            path_guard=self.check_path,
            search_scope=self.search_scope,
            empty_missing_root=True,
        )
        self.backend = ScopedBackend(inner, self.root, read_only=False, path_guard=self.check_path)

    def configure(self, plan_id):
        if not re.fullmatch(r"[a-f0-9]{32}", plan_id):
            raise ToolError("invalid_plan_id", "Plan identity must be a UUID hex string")
        self.active_name = "/" + plan_id + ".md"

    def deactivate(self):
        self.active_name = None

    def check_path(self, value):
        if value != "/" and value != self.active_name:
            raise ToolError(
                "invalid_plan_path", "Only the current Plan Markdown path is accessible"
            )

    def search_scope(self, value):
        self.check_path(value)
        return (self.active_name,) if self.active_name is not None else ()

    def virtual_path(self, plan_id):
        if not re.fullmatch(r"[a-f0-9]{32}", plan_id):
            raise ToolError("invalid_plan_id", "Plan identity must be a UUID hex string")
        return "/plans/" + plan_id + ".md"

    def storage_path(self, plan_id):
        self.virtual_path(plan_id)
        return str(self.root / (plan_id + ".md"))

    async def read(self, plan_id):
        def read():
            path = self.backend.resolve("/" + plan_id + ".md")
            try:
                with path.open("rb") as source:
                    raw = source.read(65537)
                if len(raw) > 65536:
                    raise ToolError("plan_too_large", "Plan Markdown exceeds 64 KiB")
                content = raw.decode("utf-8")
                if not content.strip():
                    raise ToolError("empty_plan", "Plan Markdown must contain a nonempty plan")
                return PlanDocument(content, hashlib.sha256(raw).hexdigest(), str(path))
            except FileNotFoundError as error:
                raise ToolError(
                    "plan_missing", "Write the active Plan Markdown before requesting approval"
                ) from error
            except (OSError, UnicodeError) as error:
                raise ToolError("plan_unreadable", "Cannot read UTF-8 Plan Markdown") from error

        return await owned_io(read)

    async def exists(self, plan_id):
        def check():
            path = self.backend.resolve("/" + plan_id + ".md")
            try:
                return stat.S_ISREG(path.stat().st_mode)
            except FileNotFoundError:
                return False
            except OSError as error:
                raise ToolError("plan_unreadable", "Cannot inspect active Plan Markdown") from error

        return await owned_io(check)
