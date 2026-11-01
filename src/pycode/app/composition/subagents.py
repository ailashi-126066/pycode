"""Composition of subagents: each child is a full AgentSession with its own history."""

from dataclasses import replace
from pathlib import Path

from pycode.kernel import EntryDraft, EventStream
from pycode.subagents import Subagent, child_prompt
from pycode.tools import ReadOnlyToolGate


def child_config(config, agent: Subagent, *, interactive: bool):
    return replace(
        config,
        workspace=Path(agent.workspace),
        read_only=agent.mode == "readonly",
        # Worktree commands must run inside an available OS sandbox.
        isolated_worker=agent.mode == "worktree",
        allow_delegation=False,
        agent_id="agent:" + agent.agent_id,
        memory_mode="off",
        max_turns=agent.max_turns,
        sandbox=replace(config.sandbox, mode="on") if agent.mode == "worktree" else config.sandbox,
        # 11.1 section 36 Q2: with nobody to answer, approval-needing calls fail instead.
        tool_gate=config.tool_gate if interactive else ReadOnlyToolGate(),
        # 11.1 section 40: in the TUI the loop question goes to the main session.
        loop_asks_user=interactive,
        credential_workspace=config.credential_workspace or config.workspace,
    )


class ConfiguredSubagentSessions:
    def __init__(self, config, store, models, default, *, interactive, session_opener, modules):
        self.config, self.store, self.models, self.default = config, store, models, default
        self.interactive, self.session_opener, self.modules = interactive, session_opener, modules

    async def open(self, agent, consumer):
        config = child_config(self.config, agent, interactive=self.interactive)
        # The child starts on the main session's current model.
        provider = await self.models.restore(self.default)
        session = await self.session_opener(
            config,
            (provider,),
            agent.session_id,
            create=True,
            consumers=(consumer,),
            refresh_models=False,
            startup_hooks=False,
            connect_mcp=False,
            prompt_suffix=child_prompt(agent),
            modules=self.modules.scoped(config),
        )
        # Usage reports find the child's requests through this record.
        await self.store.append_entry(
            EntryDraft(
                "custom",
                data={
                    "kind": "subagent_start",
                    "agent_id": agent.agent_id,
                    "session_id": agent.session_id,
                    "mode": agent.mode,
                    "description": agent.description,
                },
                run_id=agent.run_id,
            )
        )
        return session


def build_subagents(
    config,
    store,
    session_id,
    models,
    default,
    consumers,
    event_journal,
    plans,
    *,
    interactive,
    session_opener,
    modules,
):
    # Children record requests in the main project's database, so usage can include them.
    child_settings = replace(
        config, request_database=config.request_database or modules.sessions.layout.runtime_database
    )

    def events(agent):
        # Child events reach the main session wrapped as subagent events.
        return EventStream(
            session_id,
            agent.run_id,
            consumers,
            journal=event_journal,
            agent_id=agent.agent_id,
            parent_call_id=agent.parent_call_id,
        )

    def can_write():
        return not config.read_only and (plans.state is None or plans.state.mode != "planning")

    return modules.subagents.create(
        ConfiguredSubagentSessions(
            child_settings,
            store,
            models,
            default,
            interactive=interactive,
            session_opener=session_opener,
            modules=modules,
        ),
        events,
        workspace=config.workspace,
        worktrees=modules.sessions.layout.project / "worktrees",
        max_agents=config.max_agents,
        max_turns=config.max_turns,
        can_write=can_write,
    )
