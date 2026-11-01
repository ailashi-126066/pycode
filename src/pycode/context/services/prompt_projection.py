"""Reconstruct the system prompt independently of optional feature modules."""

from pycode.kernel import Message, ProjectedEntry, ProjectionContribution, clone_value


class SystemPromptProjection:
    name = "system_prompt"
    incremental_safe = True
    # Reads both checkpoint keys: prompt_snapshot directly, and the meter's
    # joined system_prompt through the projector's pre-snapshot compat branch.
    checkpoint_keys = frozenset({"prompt_snapshot", "system_prompt"})

    def contribute(self, snapshot, path, entries, checkpoint):
        prompt = None
        source_id = ""
        for entry in path:
            if entry.type == "system_prompt":
                prompt, source_id = entry.data, entry.id
            elif entry.type == "compaction":
                prompt = entry.data.get("checkpoint", {}).get("prompt_snapshot", prompt)
                source_id = entry.id
        if prompt is None:
            return ProjectionContribution()
        return ProjectionContribution(
            (
                ProjectedEntry(
                    source_id,
                    (
                        Message(
                            "prompt:" + prompt["sha256"],
                            "system",
                            prompt["text"],
                            provider_metadata={"source_type": "system_prompt"},
                        ),
                    ),
                ),
            ),
            {"prompt_snapshot": clone_value(prompt)},
        )
