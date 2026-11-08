"""Steering: words the user (or the main agent) adds while an agent is still working.

`put` drops a message into the inbox of one thread. Before the agent's next model call,
`abefore_model` moves everything waiting there into the conversation as user messages: the
step in progress finishes first, and by then every tool call has its result, so the
conversation stays valid. A message that arrives after the last model call of a run stays in
the inbox; whoever runs the agent `take`s it when the run ends and starts the next turn with it.

One instance serves the main agent and every subagent; the inbox is kept per thread id.
A steering message is part of the turn it joins: it is marked `pycode_steer` and does not start
a turn of its own (see compaction.summarize.is_user_turn).
"""

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.config import get_config


def steer_message(text: str) -> HumanMessage:
    return HumanMessage(text, additional_kwargs={"pycode_steer": True})


class Steering(AgentMiddleware):
    def __init__(self):
        super().__init__()
        self.inbox: dict[str, list[str]] = {}  # thread id -> messages not yet delivered

    def put(self, thread_id: str, text: str) -> None:
        self.inbox.setdefault(thread_id, []).append(text)

    def take(self, thread_id: str) -> list[str]:
        """What is still waiting, removed from the inbox."""
        return self.inbox.pop(thread_id, [])

    async def abefore_model(self, state, runtime):
        texts = self.take(get_config()["configurable"]["thread_id"])
        if not texts:
            return None
        return {"messages": [steer_message(text) for text in texts]}
