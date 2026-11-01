# Agent

`AgentModule` is the construction entry for `AgentRuntime`, explicit
`RuntimeBindings`, the bound message publisher and an in-memory graph checkpointer.
The framework adapters own graph state, history translation and middleware order.

```python
from pycode.agent.module import AgentModule

agent = AgentModule()
bindings = agent.bindings(
    to_framework=to_framework,
    from_assistant=from_assistant,
    make_framework_tool=make_framework_tool,
    disclose_model_tools=discovery.disclose,
    calibrate_model_request=calibrate_model_request,
    model_request_step=model_request_step,
    settlement_middleware_factory=settlement_factory,
    run_hook=run_hook,
    framework_extensions=extensions,
    model_filters=filters,
    before_model=callbacks,
)
runtime = agent.runtime(model, bindings=bindings, backend=backend, checkpointer=agent.checkpoint())
```

Extensions own optional Skill loading and model-request policies. Their ordered factories
receive only the current context, backend and injected conversion/model-boundary
callbacks. An extension that prepares the model boundary declares that capability
explicitly; history then avoids preparing the same request twice. The agent does
not parse Skill scopes or open MCP connections. `RunContext` contains per-run
execution state and native capabilities; it is not a service locator.

Main-model text follows the task_action/message JSON contract; native tool calls remain
framework-owned. Validated task changes share the durable assistant record. The graph
carries a derived task_state, and Context rebuilds it from the active branch. UI deltas
expose only message text. See docs/context-request.md.
