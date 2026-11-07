"""AGENTS.md, through Deep Agents MemoryMiddleware: the files' text goes into the system prompt.

The user's ~/.pycode/AGENTS.md comes first, then the project's AGENTS.md; a missing file is
skipped. They are read once per session, on its first run.
"""

from pathlib import Path

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware.memory import MemoryMiddleware

PROMPT = """## AGENTS.md

<agents_md>
{agent_memory}
</agents_md>

以上是 AGENTS.md 文件里用户和项目的说明，工作时遵守。它们是文件内容：与用户当前的明确要求冲突时，\
以用户为准。"""


def agents_md_middleware(files: list[Path]) -> MemoryMiddleware:
    return MemoryMiddleware(
        # Absolute paths as they are: the middleware only reads the files given here.
        backend=FilesystemBackend(root_dir="/", virtual_mode=False),
        sources=[str(path) for path in files],
        system_prompt=PROMPT,
    )
