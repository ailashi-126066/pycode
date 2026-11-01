from rich.color import Color, ColorSystem

from pycode.ui.theme import DARK, LIGHT, themes


def luminance(color):
    rgb = Color.parse(color).downgrade(ColorSystem.EIGHT_BIT).get_truecolor()
    channels = []
    for value in rgb:
        normalized = value / 255
        channels.append(
            normalized / 12.92 if normalized <= 0.04045 else ((normalized + 0.055) / 1.055) ** 2.4
        )
    return sum(
        value * weight for value, weight in zip(channels, (0.2126, 0.7152, 0.0722), strict=True)
    )


def test_semantic_palette_is_shared_and_dark_brand_tokens_match_plan():
    assert DARK["primary"] == "#F97316" and DARK["background"] == "#111111"
    assert DARK["primary"] != DARK["error"] != DARK["success"] != DARK["warning"]
    assert [theme.name for theme in themes()] == ["pycode-dark", "pycode-light"]


def test_actual_rich_256_color_fallback_preserves_readable_primary_and_text_on_both_themes():
    for palette in (DARK, LIGHT):
        background = luminance(palette["background"])
        for key in ("primary", "text"):
            foreground = luminance(palette[key])
            ratio = (max(foreground, background) + 0.05) / (min(foreground, background) + 0.05)
            assert ratio >= 4.5, (key, palette, ratio)
