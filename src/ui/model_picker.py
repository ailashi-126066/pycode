# Pi 没有，pycode 新增
"""Scrollable provider/model picker that stays beside the prompt."""

from textual.widgets import OptionList


class ModelPicker(OptionList, can_focus=False):
    DEFAULT_CSS = """
    ModelPicker {
        display: none;
        height: auto;
        max-height: 10;
        color: $text-muted;
        border: none;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(markup=False, compact=True, **kwargs)
        self.models = ()

    def show_models(self, models):
        self.models = tuple(models)
        self.clear_options()
        self.add_options(
            model.provider + "/" + model.model_id + " · " + model.availability
            for model in self.models
        )
        self.highlighted = 0 if self.models else None
        self.display = bool(self.models)

    def hide(self):
        self.models = ()
        self.clear_options()
        self.display = False

    @property
    def selected(self):
        if self.display and self.highlighted is not None:
            return self.models[self.highlighted]
        return None

    def move(self, offset):
        if self.selected is None:
            return
        self.highlighted = max(0, min(len(self.models) - 1, self.highlighted + offset))
