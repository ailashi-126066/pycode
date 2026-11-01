# Pi 没有，pycode 新增
"""Shared semantic theme tokens; components contain no literal colors."""

from textual.theme import Theme

DARK = {
    "primary": "#F97316",
    "primary-hover": "#FB923C",
    "primary-strong": "#EA580C",
    "primary-muted": "#9A3412",
    "background": "#111111",
    "surface": "#1C1C1C",
    "text": "#F5F5F5",
    "text-muted": "#A3A3A3",
    "error": "#EF4444",
    "success": "#22C55E",
    "warning": "#F59E0B",
}
LIGHT = {
    **DARK,
    "primary": "#C2410C",
    "primary-hover": "#9A3412",
    "primary-strong": "#7C2D12",
    "background": "#FAFAFA",
    "surface": "#F3F3F3",
    "text": "#171717",
    "text-muted": "#525252",
    "error": "#B91C1C",
    "success": "#15803D",
    "warning": "#92400E",
}


def themes():
    result = []
    for name, palette, dark in (("pycode-dark", DARK, True), ("pycode-light", LIGHT, False)):
        result.append(
            Theme(
                name=name,
                primary=palette["primary"],
                foreground=palette["text"],
                background=palette["background"],
                surface=palette["surface"],
                panel=palette["surface"],
                error=palette["error"],
                success=palette["success"],
                warning=palette["warning"],
                dark=dark,
                text_alpha=1,
                variables={
                    key: palette[key]
                    for key in ("primary-hover", "primary-strong", "primary-muted", "text-muted")
                },
            )
        )
    return tuple(result)
