"""Branches are LangGraph checkpoint history. The current branch is always the newest
checkpoint of the thread; reverting or switching copies an older checkpoint to the top."""

from ..compaction.summarize import count_turns


def turn_count(state) -> int:
    return count_turns(state.values.get("messages", []))


async def current_path(agent, config) -> list:
    """Checkpoints from the newest one back to the first, following parent links."""
    by_id = {
        state.config["configurable"]["checkpoint_id"]: state
        async for state in agent.aget_state_history(config)
    }
    path, state = [], await agent.aget_state(config)
    while state is not None and state.created_at is not None:
        path.append(state)
        parent = state.parent_config
        state = by_id.get(parent["configurable"]["checkpoint_id"]) if parent else None
    return path


async def revert(agent, config, turn: int) -> None:
    """Make "the conversation right after turn N finished" the current branch.

    Later turns stay in history as another branch (see `branches`)."""
    for state in await current_path(agent, config):
        # A finished run has nothing left to execute; its turn count says which turn it ends.
        if not state.next and turn_count(state) == turn:
            await agent.aupdate_state(state.config, None)
            return
    raise ValueError(f"This session has no finished turn {turn}")


async def branches(agent, config) -> list:
    """The newest checkpoint of every branch, most recently used first."""
    states = [state async for state in agent.aget_state_history(config)]
    parents = {
        state.parent_config["configurable"]["checkpoint_id"]
        for state in states
        if state.parent_config
    }
    leaves = [s for s in states if s.config["configurable"]["checkpoint_id"] not in parents]
    return sorted(leaves, key=lambda state: state.created_at, reverse=True)


async def switch(agent, config, leaf) -> None:
    if leaf.next:
        raise ValueError(
            "That branch is waiting for approval; only finished branches can be opened"
        )
    await agent.aupdate_state(leaf.config, None)
