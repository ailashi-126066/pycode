"""Skills history: component-owned implementation."""

from collections.abc import Mapping
from typing import Any

from pycode.context import ProjectionContribution
from pycode.kernel import (
    Message,
    ProjectedEntry,
    SessionEntry,
    SessionSnapshot,
    SessionTree,
    ToolCall,
)
from pycode.skills.types import active_skills


class SkillsProjection:
    name = "skills"
    incremental_safe = False
    checkpoint_keys = frozenset({"skills"})

    def contribute(
        self,
        snapshot: SessionSnapshot,
        path: tuple[SessionEntry, ...],
        entries: tuple[ProjectedEntry, ...],
        checkpoint: Mapping[str, Any],
    ) -> ProjectionContribution:
        loaded = active_skills(path)
        pinned = []
        tree = SessionTree(snapshot.index)
        catalog = next((entry for entry in reversed(path) if entry.type == "skill_catalog"), None)
        if catalog is not None and catalog.data["text"]:
            pinned.append(
                ProjectedEntry(
                    catalog.id,
                    (
                        Message(
                            "catalog:" + catalog.id,
                            "system",
                            catalog.data["text"],
                            provider_metadata={
                                "source_type": "skill_catalog",
                                "context_config_system_prompt": False,
                            },
                        ),
                    ),
                )
            )
        for item in loaded:
            body_reference = item.get("message_entry_id")
            if body_reference is not None and any(
                entry.entry_id == body_reference for entry in entries
            ):
                continue
            source = next(
                (
                    entry
                    for entry in reversed(path)
                    if entry.type == "skill_load"
                    and entry.data["loaded_hash"] == item["loaded_hash"]
                ),
                None,
            )
            if source is None:
                source = next(
                    entry
                    for entry in reversed(path)
                    if entry.type == "compaction"
                    and any(
                        saved["loaded_hash"] == item["loaded_hash"]
                        for saved in entry.data.get("checkpoint", {}).get("skills", ())
                    )
                )
            if body_reference is not None:
                original = tree.get(body_reference).require_message()
                result = original.require_tool_result()
                call = None
                for previous in path:
                    if previous.message is not None:
                        for candidate in previous.message.tool_calls:
                            if candidate.id == result.call_id and candidate.name == "read_file":
                                call = candidate
                if call is None:
                    call = ToolCall(result.call_id, "read_file", {"file_path": item["path"]})
                replay = Message("skill-read:" + source.id, "assistant", (call,))
                pinned.append(ProjectedEntry(source.id, (replay, original)))
            else:
                pinned.append(
                    ProjectedEntry(
                        source.id,
                        (
                            Message(
                                f"skill:{item['name']}:{item['loaded_hash']}",
                                "system",
                                item.get("text", ""),
                                provider_metadata={
                                    "source_type": "skill_load",
                                    "context_config_system_prompt": False,
                                    "skill_name": item["name"],
                                },
                            ),
                        ),
                    )
                )
        return ProjectionContribution(
            tuple(pinned),
            {"skills": list(loaded)} if loaded else {},
            frozenset() if loaded else self.checkpoint_keys,
            tuple(entry.id for entry in path if entry.type == "skill_load"),
        )
