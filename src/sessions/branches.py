"""Branches are LangGraph checkpoint history. The current branch is always the newest
checkpoint of the thread; reverting or switching copies an older checkpoint to the top."""

from langchain_core.messages import AIMessage, ToolMessage

from ..compaction.summarize import count_turns, is_summary


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


async def full_history(agent, config) -> list:
    """Every message of the current branch, oldest first, including the ones compaction
    replaced: they are still in earlier checkpoints. Each summary sits where it was adopted,
    right before the first message it kept."""
    messages, seen = [], set()
    for state in reversed(await current_path(agent, config)):
        current = state.values.get("messages", [])
        for position, message in enumerate(current):
            if message.id in seen:
                continue
            seen.add(message.id)
            kept = current[position + 1] if position + 1 < len(current) else None
            if is_summary(message) and kept is not None and kept.id in seen:
                messages.insert([m.id for m in messages].index(kept.id), message)
            else:
                messages.append(message)
    return messages


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


async def settle_cancelled(agent, config) -> None:
    """After the user stops a run, answer tool calls that never got a result.

    A reply whose tool calls have no results would make the next model request invalid."""
    state = await agent.aget_state(config)
    if state.interrupts:
        return  # still waiting for approval; answering it settles those calls
    messages = state.values.get("messages", [])
    reply = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    missing = [call for call in (reply.tool_calls if reply else []) if call["id"] not in answered]
    if missing:
        results = [
            ToolMessage(
                "The user stopped the run before this tool finished; its effects are unknown.",
                tool_call_id=call["id"],
                name=call["name"],
                status="error",
            )
            for call in missing
        ]
        await agent.aupdate_state(config, {"messages": results})
