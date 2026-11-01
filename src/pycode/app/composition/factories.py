"""Small cross-component connections; feature construction remains in module.py."""

from pycode.skills.module import SkillsSettings


def build_summary_generator(config, provider, consumers, *, modules):

    async def owner_factory(selected, session_id, run_id, purpose):
        return await modules.models.owner(selected, consumers, session_id, run_id, purpose)

    return modules.context.generator(provider, owner_factory, modules.models.records)


def build_memory_service(config, history, provider=None, consumers=(), *, modules):
    updater = (
        build_memory_updater(config, provider, consumers, modules=modules)
        if provider is not None
        else None
    )
    return modules.memory.service(history, updater=updater)


def build_memory_updater(config, provider, consumers=(), *, modules):

    async def owner_factory(selected, session_id, run_id, purpose, **options):
        return await modules.models.owner(
            selected, consumers, session_id, run_id, purpose, **options
        )

    def facade_factory(owner, prefix, **options):
        return modules.models.metered(owner, logical_prefix=prefix, **options)

    return modules.memory.updater(provider, owner_factory, facade_factory)


def build_outputs(modules, session_id):
    return modules.tools.output_store(session_id)


def skill_settings(config):
    return SkillsSettings(
        config.workspace, config.user_data, config.project_trusted, config.skill_index_budget
    )
