"""Shared visual language for the dashboard: palette, Plotly layout defaults,
and the small helpers both pages use.

Colours are not chosen by eye. The two-series categorical pair below was run
through a CVD/contrast validator against the light chart surface and clears
every gate (worst-pair ΔE 24.7 protan, 33.6 normal vision, both >= 3:1
contrast on the surface), so baseline vs. AI stays distinguishable for
colour-blind readers and in greyscale print.

Two deliberate rules, both of which the first version of this dashboard broke:

  - **No dual-axis charts.** Outdoor temperature and humidity are two
    measures on different scales; putting them on one plot with two y-axes
    invites the reader to see a correlation the geometry invented. They get
    two stacked charts sharing an x-axis instead.

  - **Colour follows the entity, not its rank.** "Baseline" is always blue
    and "AI closed-loop" always orange, on every chart and every site, so
    switching sites never repaints a series.

The app pins Streamlit to its light theme (.streamlit/config.toml) so what
renders matches the surface the palette was validated against.
"""

# --- Categorical slots (validated pair) ---
BASELINE = "#2a78d6"  # slot 1, blue
AI = "#eb6834"  # slot 2, orange

# --- Supporting ink / surface tokens ---
SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
TEXT_MUTED = "#8a8880"
GRID = "#e8e6e1"  # hairline, one shade off the surface -- solid, never dashed
BAND = "#1baf7a"  # aqua, used only for the PMV comfort band shading

FONT = "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"

# Weather series get their own single-hue treatment -- they are context, not
# part of the baseline/AI comparison, so they must not borrow either's colour.
WEATHER_TEMP = "#256abf"  # sequential blue, step 500
WEATHER_RH = "#184f95"  # same hue, darker step -- one hue, two magnitudes


def style(fig, *, height=340, ylabel="", xlabel="", legend=True):
    """Apply the house layout: recessive solid hairline grid, generous
    padding, unified crosshair hover, legend across the top."""
    fig.update_layout(
        height=height,
        font=dict(family=FONT, size=13, color=TEXT_SECONDARY),
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        # Left/bottom are left to automargin below rather than fixed: a fixed
        # small margin clipped the y-axis title ("...ooling setpoint (C)") at
        # narrower widths, and a fixed large one wastes space on charts whose
        # tick labels are short.
        margin=dict(l=8, r=16, t=48 if legend else 16, b=8),
        hovermode="x unified",
        hoverlabel=dict(bgcolor="#ffffff", font_size=12, font_family=FONT, bordercolor=GRID),
        showlegend=legend,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0,
            title=None,
            font=dict(size=12, color=TEXT_SECONDARY),
        ),
    )
    axis = dict(
        showgrid=True,
        gridcolor=GRID,
        gridwidth=1,
        zeroline=False,
        linecolor=GRID,
        ticks="",
        automargin=True,  # reserve room for the axis title instead of clipping it
        tickfont=dict(size=11, color=TEXT_MUTED),
        title_font=dict(size=12, color=TEXT_SECONDARY),
    )
    fig.update_xaxes(**axis, title_text=xlabel)
    # A little headroom so a series that peaks at the top of its range (the
    # baseline setback schedule does) doesn't sit flush against the plot edge.
    fig.update_yaxes(**axis, title_text=ylabel)
    return fig


def pad_y(fig, series, frac=0.08):
    """Expand the y-range by a fraction of its span on both sides."""
    lo, hi = float(min(series)), float(max(series))
    pad = (hi - lo) * frac or 0.5
    fig.update_yaxes(range=[lo - pad, hi + pad])
    return fig


def line(fig_add, x, y, name, color, dash=None, width=2):
    """Thin 2px marks. Saturated fills are for small accents, never large
    blocks, so every series here is a line."""
    import plotly.graph_objects as go

    return go.Scatter(
        x=x,
        y=y,
        name=name,
        mode="lines",
        line=dict(color=color, width=width, dash=dash),
        hovertemplate="%{y:.2f}<extra>" + name + "</extra>",
    )


def to_datetime(df, year):
    """EnergyPlus stamps rows ' MM/DD  HH:MM:SS' with hour 24 meaning
    midnight *ending* the day, which no date parser accepts directly. Build
    the timestamp from parts so the axis can show real dates instead of an
    opaque timestep counter."""
    import pandas as pd

    raw = df["Date/Time"].astype(str).str.strip()
    parts = raw.str.extract(r"(\d+)/(\d+)\s+(\d+):(\d+)").astype(int)
    base = pd.to_datetime(
        dict(year=year, month=parts[0], day=parts[1])
    )
    return base + pd.to_timedelta(parts[2], unit="h") + pd.to_timedelta(parts[3], unit="m")
