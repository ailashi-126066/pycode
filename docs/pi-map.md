# Pi → pycode map

Pi source: https://github.com/earendil-works/pi/tree/2c8d1fe

## Agent loop (`packages/agent/src/agent-loop.ts` `runLoop`)

LangGraph runs the loop; `agent/adapters/history.py::HistoryMiddleware` supplies Pi's
`AgentLoopConfig` hooks, and `agent/domain/run_state.py::RunState` makes the loop state
explicit (`starting → model_turn ⇄ tool_turn → awaiting_input → verifying → finished`).

| Pi | pycode | Framework mechanism |
|---|---|---|
| `prepareNextTurn` | `HistoryMiddleware.abefore_model` (compaction, eviction, turn budget) | middleware `before_model`, `jump_to="end"` |
| `prepareRequest` + `streamAssistantResponse` | `HistoryMiddleware.awrap_model_call` | middleware `wrap_model_call` |
| `finishTurn` | `HistoryMiddleware.aafter_model` (turn without tools), `aafter_agent` (follow-ups) | middleware `after_model` / `after_agent`, `jump_to="model"` |
| `executeToolCalls` | `tools/adapters/settlement.py::ToolSettlementMiddleware.ordered_call` | tools node + `wrap_tool_call` |
| `turn_end` | `history.py::end_turn`, from every RunState transition that leaves a turn | — |
| `agent_end` | `AgentRuntime.finish`: `run_end` record → `agent_end` event → `run_end` Hook | after `graph.ainvoke` returns or raises |
| awaited approval | `interrupt` in the permission gate, answered by `AgentRuntime.resume` | `interrupt` / `Command(resume)` |

## Steering polls (`getSteeringMessages`)

`history.py::poll_steering` runs at the same three points as Pi (`agent-loop.ts` 185, 214, 304):

1. loop start: `abefore_agent`;
2. turn end: `aafter_model` (no tool calls) or `abefore_model` (after a tool turn);
3. after preparation in `abefore_model`, only when the turn-end poll took nothing.
