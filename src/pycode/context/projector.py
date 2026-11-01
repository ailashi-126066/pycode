# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/session-manager.ts
# Adaptation: plain Python messages, provenance/hash, explicit compaction checkpoint and text edits.
"""Canonical branch and compaction projection used by every context consumer."""

from dataclasses import replace

from pycode.context.compaction import repair_tool_pairing, retained_context_entries
from pycode.context.prompts import system_prompt_contribution
from pycode.kernel import (
    Feature,
    Message,
    ProjectedContext,
    ProjectedEntry,
    SessionEntry,
    SessionSnapshot,
    SessionTree,
    StorageError,
    clone_value,
    freeze_value,
)


# Pi: declareToolChanges (agent-loop.ts)
def declare_tool_changes(path: tuple[SessionEntry, ...]) -> dict[str, Message]:
    """A system message names the model-visible tools added or removed since the last request."""
    declarations = {}
    previous = None
    for entry in path:
        if entry.type != "tool_set_change" or entry.data["source"] != "effective_model":
            continue
        names = [tool["name"] for tool in entry.data["tools"]]
        if previous is not None:
            added = [name for name in names if name not in previous]
            removed = [name for name in previous if name not in names]
            if added or removed:
                lines = ["The available tools changed."]
                if added:
                    lines.append("Added: " + ", ".join(added))
                if removed:
                    lines.append("Removed: " + ", ".join(removed))
                declarations[entry.id] = Message(
                    f"tools:{entry.id}",
                    "system",
                    "\n".join(lines),
                    timestamp=entry.timestamp,
                    provider_metadata={"source_type": "tool_set_change"},
                )
        previous = names
    return declarations


class ContextProjector:
    PROJECTION_VERSION = 1
    ORDINARY_STATES = frozenset(
        {
            "custom",
            "input_queued",
            "session_metadata",
        }
    )

    def __init__(self, features: tuple[Feature, ...] = ()):
        self._features = features
        self._feature_keys = frozenset().union(*(feature.checkpoint_keys for feature in features))
        self._session_id = None
        self._key = None
        self._cached = None
        self._raw_messages = ()
        self._tree = None

    def incremental(self, snapshot):
        if self._cached is None or self._key is None:
            return None
        if snapshot.revision <= self._key[1]:
            return None
        added = []
        tree = SessionTree(snapshot.index)
        current = snapshot.leaf_id
        while current != self._cached.leaf_id:
            if current is None:
                return None
            entry = tree.get(current)
            if entry.type not in self.ORDINARY_STATES and entry.type not in {
                "message",
                "custom_message",
                "hook_context",
            }:
                return None
            if entry.type == "message" and entry.require_message().role == "system":
                return None
            if (
                entry.message is not None
                and entry.message.tool_result is not None
                and self._feature_keys & entry.message.tool_result.details.keys()
            ) or (entry.type == "custom" and entry.data["kind"] in self._feature_keys):
                # New feature state (a task board) changes the checkpoint, not just the tail.
                return None
            if (
                entry.type == "message"
                and entry.require_message().model_selection is not None
                and entry.require_message().model_selection != self._cached.selection
            ):
                return None
            added.append(entry)
            current = entry.parent_id
        added.reverse()
        contributions = []
        tail = []
        for entry in added:
            messages = tuple(freeze_value(message) for message in self.messages_for(entry))
            contributions.append(ProjectedEntry(entry.id, messages))
            tail.extend(messages)
        if tail:
            self._raw_messages = self._raw_messages + tuple(tail)
            messages = repair_tool_pairing(self._raw_messages)
        else:
            messages = self._cached.messages
        return ProjectedContext(
            snapshot.leaf_id,
            self._cached.entries + tuple(contributions),
            messages,
            self._cached.selection,
            self._cached.thinking,
            self._cached.checkpoint,
        )

    # Pi: transformContext — compaction, eviction, injected context and orphan-call results.
    def transform_context(self, snapshot: SessionSnapshot) -> ProjectedContext:
        self._tree = SessionTree(snapshot.index)
        if self._session_id != snapshot.header.session_id:
            self._session_id = snapshot.header.session_id
            self._key = None
            self._cached = None
        key = (snapshot.leaf_id, snapshot.revision, self.PROJECTION_VERSION)
        if key == self._key and self._cached is not None:
            return self._cached
        incremental = self.incremental(snapshot)
        if incremental is not None:
            self._key = key
            self._cached = incremental
            return incremental
        path = SessionTree(snapshot.index).path(snapshot.leaf_id)
        selection, thinking = self.model_context(path)
        contributions, checkpoint, raw_messages = self.projected_entries(path)
        messages = repair_tool_pairing(raw_messages)
        pinned = []
        contributors = (system_prompt_contribution, *(f.contribute for f in self._features))
        for contribute in contributors:
            contribution = contribute(snapshot, path, checkpoint)
            if contribution is None:
                continue
            for key_to_remove in contribution.checkpoint_removals:
                checkpoint.pop(key_to_remove, None)
            checkpoint.update(contribution.checkpoint_updates)
            pinned.extend(contribution.entries)
        return self.cache_projection(
            snapshot,
            key,
            selection,
            thinking,
            checkpoint,
            contributions,
            messages,
            raw_messages,
            pinned,
        )

    def model_context(self, path):
        selection = None
        thinking = "off"
        for entry in path:
            if entry.type == "model_change":
                selection = entry.selection
            elif entry.type == "thinking_level_change":
                thinking = entry.data["thinking"]
            elif entry.type == "message" and entry.require_message().role == "assistant":
                if entry.require_message().model_selection is not None:
                    selection = entry.require_message().model_selection
        return selection, thinking

    def projected_entries(self, path):
        context_entries = retained_context_entries(path)
        edits = {
            entry.data["target_id"]: entry
            for entry in context_entries
            if entry.type == "context_edit"
        }
        declarations = declare_tool_changes(path)
        contributions = []
        checkpoint = {}
        for index, entry in enumerate(context_entries):
            if entry.type == "compaction" and index > 0:
                messages = ()
            elif entry.id in declarations:
                messages = (declarations[entry.id],)
            else:
                messages = self.messages_for(entry)
            edit = edits.get(entry.id)
            if edit is not None:
                messages = self.edit_messages(messages, edit.data["replacement"])
            if entry.type == "compaction" and index == 0:
                checkpoint = clone_value(entry.data.get("checkpoint", {}))
            contributions.append(ProjectedEntry(entry.id, messages))
        for entry in path:
            if entry.type == "tool_set_change" and entry.data["source"] == "effective_model":
                checkpoint["model_tools"] = clone_value(entry.data["tools"])
                checkpoint["harness_system_hash"] = entry.data.get("harness_system_hash", "")
                checkpoint["harness_system_tokens"] = entry.data.get("harness_system_tokens", 0)
        flattened = []
        for contribution in contributions:
            flattened.extend(contribution.messages)
        messages = tuple(flattened)
        raw_messages = messages
        return contributions, checkpoint, raw_messages

    def cache_projection(
        self,
        snapshot,
        key,
        selection,
        thinking,
        checkpoint,
        contributions,
        messages,
        raw_messages,
        pinned,
    ):
        pinned_by_id = {}
        for item in pinned:
            pinned_by_id.setdefault(item.entry_id, []).extend(item.messages)
        for item in contributions:
            if item.entry_id in pinned_by_id:
                pinned_by_id[item.entry_id].extend(item.messages)
        contributions = [
            *[ProjectedEntry(entry_id, tuple(items)) for entry_id, items in pinned_by_id.items()],
            *[item for item in contributions if item.entry_id not in pinned_by_id],
        ]
        pinned_messages = []
        for item in pinned:
            pinned_messages.extend(item.messages)
        messages = tuple(pinned_messages) + messages
        projected = ProjectedContext(
            snapshot.leaf_id,
            tuple(contributions),
            messages,
            selection,
            thinking,
            checkpoint,
        )
        self._cached = freeze_value(projected)
        self._key = key
        # The incremental path re-pairs these; repair_tool_pairing freezes its copy.
        self._raw_messages = tuple(pinned_messages) + raw_messages
        return self._cached

    def messages_for(self, entry: SessionEntry) -> tuple[Message, ...]:
        if entry.type == "message":
            return (freeze_value(entry.require_message()),)
        if entry.type == "input_expired":
            if self._tree is None:
                raise StorageError("projection_not_started", "Project the snapshot first")
            original = self._tree.get(entry.data["input_entry_id"])
            for saved in self._tree.path(entry.parent_id):
                if (
                    saved.type == "message"
                    and saved.require_message().id == original.require_message().id
                ):
                    return ()
            return (
                Message(
                    "recovered-input:" + original.id,
                    "user",
                    "[Queued instruction recovered after interruption; old execution expired]\n"
                    + original.require_message().text,
                    provider_metadata={
                        "source_type": "input_expired",
                        "input_entry_id": original.id,
                    },
                ),
            )
        if entry.type in {"custom_message", "hook_context"}:
            return (
                Message(
                    entry.id,
                    "custom",
                    entry.data["text"],
                    timestamp=entry.timestamp,
                    provider_metadata={"source_type": entry.type},
                ),
            )
        if entry.type in {"compaction", "branch_summary"}:
            summary = Message(
                f"summary:{entry.id}",
                "compaction_summary" if entry.type == "compaction" else "branch_summary",
                entry.data["summary"],
                timestamp=entry.timestamp,
                provider_metadata={"source_type": entry.type},
            )
            prompt = entry.data.get("checkpoint", {}).get("system_prompt")
            if (
                entry.type == "compaction"
                and prompt is not None
                and not entry.data.get("checkpoint", {}).get("prompt_snapshot")
            ):
                return (
                    Message(f"system:{entry.id}", "system", prompt, timestamp=entry.timestamp),
                    summary,
                )
            return (summary,)
        return ()

    def edit_messages(
        self, messages: tuple[Message, ...], replacement: str | None
    ) -> tuple[Message, ...]:
        if replacement is None:
            return ()
        edited = []
        for message in messages:
            if message.role == "tool_result":
                edited.append(
                    replace(
                        message,
                        tool_result=replace(message.require_tool_result(), text=replacement),
                    )
                )
            elif message.role == "system":
                edited.append(message)
            else:
                # Replacing the content also drops an assistant's thinking and tool calls.
                edited.append(replace(message, content=replacement, attachments=()))
        return tuple(edited)
