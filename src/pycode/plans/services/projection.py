"""Reconstruct this feature's historical model context without opening resources."""

from pycode.kernel import Message, ProjectedEntry, ProjectionContribution, clone_value


class PlanProjection:
    name = "plans"
    incremental_safe = True
    checkpoint_keys = frozenset({"plan"})

    def contribute(self, snapshot, path, entries, checkpoint):
        pinned = []
        updates = {}
        plan_entry = None
        for entry in path:
            if (
                entry.type == "plan_state"
                and entry.data["session_id"] == snapshot.header.session_id
            ):
                plan_entry = entry
        if plan_entry is not None and plan_entry.data["mode"] != "completed":
            plan_state = plan_entry.data
            updates["plan"] = clone_value(plan_state)
            reminder = (
                f"Plan mode={plan_state['mode']}; active Markdown={plan_state['virtual_path']}. "
                f"File exists={plan_state['document_exists']}. "
            )
            if plan_state["mode"] == "planning":
                reminder += (
                    "The project is read only. Only write the exact active Plan Markdown. "
                    "End it with a ```pycode-tasks block holding TaskWrite arguments as JSON, "
                    "then call ExitPlanMode to request approval."
                )
            else:
                reminder += (
                    "Execute the approved Plan within this run by following its task board; "
                    "the Plan Markdown is read only."
                )
            pinned.append(
                ProjectedEntry(
                    plan_entry.id, (Message("plan:" + plan_state["plan_id"], "system", reminder),)
                )
            )

        return ProjectionContribution(tuple(pinned), updates, self.checkpoint_keys)
