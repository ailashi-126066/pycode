"""Skills, through Deep Agents SkillsMiddleware.

The system prompt lists each skill's name and description; the model reads a SKILL.md with
`read` when a task matches (Files lets `read` reach the skill directories, read-only). /skill
asks for one directly: its SKILL.md is pinned into the conversation as its own message (see
`compaction.summarize.is_skill`). The list is read once per session, on its first run.
"""

from pathlib import Path

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware.skills import SkillsMiddleware

PROMPT = """## Skills

Skills 是针对某类任务的操作说明，下面只列出名字和用途。任务符合某个 Skill 时，先用 read 读取它的 \
SKILL.md 全文，再按说明去做。用户用 /skill 指定的 Skill 已经放进对话，不用再读。Skill 附带的脚本\
用绝对路径通过 bash 运行。

{skills_locations}{skills_load_warnings}

{skills_list}"""


def skills_middleware(directories: dict[str, Path]) -> SkillsMiddleware:
    """`directories` maps a label to a directory; later ones win on equal skill names."""
    return SkillsMiddleware(
        # Absolute paths as they are: the middleware only reads the directories given here.
        backend=FilesystemBackend(root_dir="/", virtual_mode=False),
        # Missing directories would show up as load warnings in every prompt.
        sources=[(str(path), label) for label, path in directories.items() if path.is_dir()],
        system_prompt=PROMPT,
    )


async def list_skills(middleware: SkillsMiddleware) -> list[dict]:
    """The skills on disk now (name, description, path), for /skills and /skill."""
    loaded = await middleware.abefore_agent({}, None, None)
    return loaded["skills_metadata"]
