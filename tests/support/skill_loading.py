"""Service-level Skill snapshot fixtures through the real read-only Backend.

End-to-end framework disclosure is covered in test_final_skills.py. These
fixtures exercise the same native file read and durable recorder boundaries.
"""

from pathlib import Path
from uuid import uuid4

from pycode.kernel.domain.messages import Message, ToolCall, ToolResult
from pycode.kernel.domain.values import plain_data
from pycode.skills.framework import SkillBackend, SkillLoadRecorder
from pycode.skills.types import active_skills
from pycode.tools.adapters.backend import PycodeBackend


async def read_skill(store, catalog, name):
    index = await catalog.scan()
    selected = None
    for entry in index.entries:
        if entry.name == name:
            selected = entry
            break
    assert selected is not None
    path = catalog.virtual_path(selected)
    backend = SkillBackend(PycodeBackend(Path(selected.path).parent.parent, read_only=True))
    read = await backend.aread(
        "/" + Path(selected.path).parent.name + "/SKILL.md", offset=0, limit=2000
    )
    assert read.error is None, read.error
    call_id = uuid4().hex
    await store.append_message(
        Message(
            uuid4().hex,
            "assistant",
            content=(ToolCall(call_id, "read_file", {"file_path": path}),),
        )
    )
    text = read.file_data["content"]
    result = ToolResult(call_id, "read_file", text, details={"protected_content": True})
    entry_id = await store.append_message(Message(uuid4().hex, "tool_result", tool_result=result))
    await SkillLoadRecorder(store, catalog).record(path, result, entry_id)
    loaded = active_skills((await store.snapshot()).entries)[-1]
    return {**plain_data(loaded), "text": text}
