"""Visual tokens for the dashboard.

Colours come from a validated reference data-viz palette: the categorical
slots pass colour-vision-deficiency separation checks in both light and dark
mode, and the dark mode uses its own validated steps rather than an automatic
inversion. Strategies own fixed slots (colour follows the entity, never its
rank), so IMMEDIATE is always slot 1, TWAP slot 2, and so on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

import streamlit as st

FONT: Final = 'system-ui, -apple-system, "Segoe UI", sans-serif'
STRATEGY_ORDER: Final = ("IMMEDIATE", "TWAP", "VWAP", "POV")


@dataclass(frozen=True)
class Palette:
    mode: Literal["light", "dark"]
    ink: str
    ink_secondary: str
    muted: str
    grid: str
    baseline: str
    surface: str
    series: tuple[str, ...]
    deemphasis: str
    wash: str


LIGHT: Final = Palette(
    mode="light",
    ink="#0b0b0b",
    ink_secondary="#52514e",
    muted="#898781",
    grid="#e1e0d9",
    baseline="#c3c2b7",
    surface="#fcfcfb",
    series=(
        "#2a78d6",
        "#eb6834",
        "#1baf7a",
        "#eda100",
        "#e87ba4",
        "#008300",
        "#4a3aa7",
        "#e34948",
    ),
    deemphasis="#c3c2b7",
    wash="rgba(42, 120, 214, 0.08)",
)

DARK: Final = Palette(
    mode="dark",
    ink="#ffffff",
    ink_secondary="#c3c2b7",
    muted="#898781",
    grid="#2c2c2a",
    baseline="#383835",
    surface="#1a1a19",
    series=(
        "#3987e5",
        "#d95926",
        "#199e70",
        "#c98500",
        "#d55181",
        "#008300",
        "#9085e9",
        "#e66767",
    ),
    deemphasis="#52514e",
    wash="rgba(57, 135, 229, 0.12)",
)


def active_palette() -> Palette:
    """The palette for the viewer's current Streamlit theme (light by default)."""
    theme = st.context.theme
    return DARK if theme is not None and theme.type == "dark" else LIGHT


def strategy_color(palette: Palette, strategy: str) -> str:
    """Fixed slot per strategy; anything else (e.g. the hindsight oracle) is de-emphasised."""
    if strategy in STRATEGY_ORDER:
        return palette.series[STRATEGY_ORDER.index(strategy)]
    return palette.deemphasis


def _axis(palette: Palette, title: str | None) -> dict[str, Any]:
    return {
        "title": {"text": title or "", "font": {"color": palette.ink_secondary, "size": 12}},
        "showgrid": True,
        "gridcolor": palette.grid,
        "gridwidth": 1,
        "zeroline": False,
        "showline": True,
        "linecolor": palette.baseline,
        "linewidth": 1,
        "ticks": "outside",
        "tickcolor": palette.baseline,
        "tickfont": {"color": palette.muted, "size": 11},
        "automargin": True,
    }


def layout(
    palette: Palette,
    *,
    height: int = 340,
    x_title: str | None = None,
    y_title: str | None = None,
    legend: bool = True,
) -> dict[str, Any]:
    """Recessive chrome: hairline solid grid, muted ticks, legend above the plot."""
    return {
        "height": height,
        # Room for tick labels and axis titles; automargin grows it for long labels.
        "margin": {"l": 64, "r": 32, "t": 48 if legend else 16, "b": 56},
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "font": {"family": FONT, "color": palette.ink, "size": 12},
        "xaxis": _axis(palette, x_title),
        "yaxis": _axis(palette, y_title),
        "showlegend": legend,
        "legend": {
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "xanchor": "left",
            "x": 0,
            "font": {"color": palette.ink_secondary},
        },
        "hoverlabel": {"font": {"family": FONT}},
    }
