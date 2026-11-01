"""Summary background-model lifetime and lost-request reconciliation."""

from dataclasses import replace


class ConfiguredSummaryGenerator:
    def __init__(self, provider, model_factory, records_factory, workflow_factory):
        self.provider = replace(
            provider, supports_tools=False, max_output_tokens=min(provider.max_output_tokens, 8192)
        )
        self.model_factory = model_factory
        self.records_factory = records_factory
        self.workflow_factory = workflow_factory

    async def reconcile_lost(self, source):
        records = self.records_factory()
        try:
            await records.interrupt_unsettled(source.session_id, source.job_id)
        finally:
            await records.aclose()

    async def generate(self, source):
        owner = await self.model_factory(self.provider, source.session_id, source.job_id, "summary")
        try:
            return await self.workflow_factory(owner.model).run(source, owner.requests)
        finally:
            await owner.aclose()
