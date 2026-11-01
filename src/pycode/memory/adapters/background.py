"""One owned model request scope for LangMem consolidation."""

from dataclasses import replace

from pycode.context import InputBudget
from pycode.memory.adapters.memory_update import MemoryConsolidator


class ConfiguredMemoryUpdater:
    """Every consolidation owns one metered background model lifetime."""

    def __init__(self, provider, model_factory, facade_factory):
        # Thinking is always off for memory: LangMem forces a tool choice, which
        # DeepSeek rejects with thinking enabled. Empty thinking makes the factory send
        # {"type": "disabled"} to DeepSeek and omit thinking/reasoning for other providers.
        self.provider = replace(
            provider,
            thinking={},
            reasoning_effort=None,
            max_output_tokens=min(provider.max_output_tokens, 4096),
        )
        self.model_factory = model_factory
        self.facade_factory = facade_factory

    def facade(self, owner, prefix):
        budget = InputBudget.create(self.provider.context_window, self.provider.max_output_tokens)
        return self.facade_factory(
            owner,
            prefix,
            input_limit=min(16000, budget.input_limit),
            max_calls=24,
        )

    async def consolidate(self, store, namespace, summary, messages, session_id, run_id):
        owner = await self.model_factory(self.provider, session_id, run_id, "memory_extract")
        try:
            consolidator = MemoryConsolidator(self.facade(owner, "memory_manager"))
            return await consolidator(store, namespace, summary, messages)
        finally:
            await owner.aclose()
