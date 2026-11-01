"""Service-level Skill snapshot fixtures through the real read-only Backend.

End-to-end framework disclosure is covered in test_final_skills.py. These
fixtures exercise the same native file read and durable recorder boundaries.
"""

from pathlib import Path
from uuid import uuid4

from pycode.kernel import Message, SessionTree, ToolCall, ToolResult, plain_data
from pycode.skills.skills import SkillBackend
from pycode.skills.types import active_skills


async def read_skill(store, catalog, name):
    index = await catalog.scan()
    selected = None
    for entry in index.entries:
        if entry.name == name:
            selected = entry
            break
    assert selected is not None
    path = catalog.virtual_path(selected)
    backend = SkillBackend(Path(selected.path).parent.parent)
    read = await backend.aread(
        "/" + Path(selected.path).parent.name + "/SKILL.md", offset=0, limit=2000
    )
    assert read.error is None, read.error
    call_id = uuid4().hex
    await store.append_message(
        Message(
            uuid4().hex,
            "assistant",
            content=(ToolCall(call_id, "read", {"path": path}),),
        )
    )
    text = read.file_data["content"]
    skill = {
        "name": selected.name,
        "path": path,
        "content_hash": selected.content_hash,
        "allowed_tools": list(selected.allowed_tools),
    }
    result = ToolResult(call_id, "read", text, details={"protected_content": True, "skill": skill})
    await store.append_message(Message(uuid4().hex, "tool_result", tool_result=result))
    snapshot = await store.snapshot()
    loaded = active_skills(SessionTree(snapshot.index).path(snapshot.leaf_id))[-1]
    return {**plain_data(loaded), "text": text}
