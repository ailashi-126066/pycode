"""Context policy and framework adapters assembled from explicit model capabilities."""

from pathlib import Path

from pycode.context.adapters.background import ConfiguredSummaryGenerator
from pycode.context.adapters.storage.prompts import InstructionFileBackend, ProjectPromptSource
from pycode.context.adapters.storage.summaries import FileSummaryStore
from pycode.context.adapters.summary import SummaryWorkflow, prepare_summary
from pycode.context.services.context_projector import ContextProjector
from pycode.context.services.prompt_projection import SystemPromptProjection
from pycode.context.services.prompts import PromptService
from pycode.context.services.summary import SummaryService
from pycode.context.services.tool_eviction import ToolEvictionService
from pycode.kernel import StorageError


class ContextModule:
    def __init__(self, *, request_step, stop_reason):
        self.request_step = request_step
        self.stop_reason = stop_reason

    def projector(self, contributors=None):
        return ContextProjector(contributors)

    def prompt_projection(self):
        return SystemPromptProjection()

    def workflow(self, model):
        return SummaryWorkflow(model, request_step=self.request_step, stop_reason=self.stop_reason)

    def generator(self, provider, model_factory, records_factory):
        return ConfiguredSummaryGenerator(provider, model_factory, records_factory, self.workflow)

    def summary(self, history, projector, directory, generator, settings, events, **options):
        return SummaryService(
            history,
            projector,
            FileSummaryStore(directory),
            generator,
            settings,
            events,
            preparation=prepare_summary,
            **options,
        )

    def eviction(self, history, projector, budget, events):
        return ToolEvictionService(history, projector, budget, events)

    def prompt(self, history, workspace, *, suffix="", sections=()):
        return PromptService(
            history,
            ProjectPromptSource(
                workspace,
                Path(__file__).parent / "resources/system-prompt.txt",
                suffix=suffix,
                sections=sections,
            ),
        )

    def instructions(self, workspace, user_data, *, trusted=False):
        sources = [user_data / "AGENTS.md"]
        if trusted:
            project = workspace / "AGENTS.md"
            if not project.resolve().is_relative_to(workspace.resolve()):
                raise StorageError(
                    "instructions_outside_workspace", "Project AGENTS.md escapes the workspace"
                )
            sources.append(project)
        paths = tuple(dict.fromkeys(str(path.resolve()) for path in sources if path.is_file()))
        return paths, InstructionFileBackend(paths)
