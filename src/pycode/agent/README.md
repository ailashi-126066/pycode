# Agent

Pi-style agent loop on Deep Agents, with explicit dependencies assembled by app.

- runtime.py: AgentRuntime(model, *, bindings, middleware=(), context_transforms=(), tool_filters=(), before_finish=None, assign_title=None, checkpointer=None, backend=None, subagents=(), instructions=(), instruction_backend=None).
- loop.py: history, turns, queued inputs and durable model requests.
- context.py: RunContext, RunState, next_turn and PycodeState.
- boundaries.py: actual model tool filtering and streamed/durable message events.
- calibration.py: effective model tool set and system prompt accounting.

App refreshes services and registers tools before constructing the runtime. Runtime receives actual
framework middleware and callback functions, never a generic service list. Context transforms run
in supplied order; tool filters run after the core read-only filter. The optional before_finish
callback performs a task-board exit check before the run closes its input boundary. RuntimeBindings
contains the actual model/tool adapters and run_hook callback. No service discovery or lifecycle
framework is implemented here.
