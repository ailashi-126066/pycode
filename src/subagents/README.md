# Subagents

SubagentManager(open_child, worktrees, events, *, workspace, max_agents, max_turns, can_write)
opens concrete child sessions through an async function supplied by app. GitWorktrees is the concrete
storage implementation. App registers subagent_tools(manager), transform_context and on_event.

start, send, answer and stop preserve foreground/background runs and approvals. view exposes the
UI task list. wait_background supports headless runs; aclose cancels children and closes sessions.
No factory module or single-implementation Protocol is needed.
