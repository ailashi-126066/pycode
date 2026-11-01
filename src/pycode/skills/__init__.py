# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/skills.ts
"""Skills: discover, load through the read tool, author and install local instructions."""

from pycode.skills.install import GitHubSkillInstaller
from pycode.skills.skills import Skills, SkillsSession
from pycode.skills.store import FileSkillCatalog
from pycode.skills.types import SkillDocument, SkillEntry, SkillIndex

__all__ = [
    "FileSkillCatalog",
    "GitHubSkillInstaller",
    "SkillDocument",
    "SkillEntry",
    "SkillIndex",
    "Skills",
    "SkillsSession",
]
