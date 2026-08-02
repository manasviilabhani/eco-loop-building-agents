"""Shared visual language for the dashboard: palette, Plotly layout defaults,
and the small helpers both pages use.

Colours are not chosen by eye. The palette is a single warm neutral ramp --
near-white through greige and brown to near-black -- so the two series are
separated by *lightness* rather than hue. Run through a CVD/contrast
validator that pair measures ΔE 34.8 deutan, 34.9 tritan, 35.0 normal
vision, far above the 15 floor, which is also why it survives greyscale
print. Being a sequential ramp it deliberately fails the categorical chroma
floor ("reads gray" is the intent), so identity never rests on colour alone:
baseline is the faint dashed line, AI the strong solid one, on every chart.

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
BASELINE = "#a89f92"  # greige, always dashed
AI = "#4a382c"  # dark brown, always solid

# --- Supporting ink / surface tokens ---
SURFACE = "#f7f7f6"
TEXT_PRIMARY = "#241d19"
TEXT_SECONDARY = "#6a513d"
TEXT_MUTED = "#8b8073"
GRID = "#e0e0df"  # hairline, one shade off the surface -- solid, never dashed
BAND = "#8b8073"  # taupe, used only for the PMV comfort band shading

FONT = "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"

# Weather series get their own single-hue treatment -- they are context, not
# part of the baseline/AI comparison, so they must not borrow either's colour.
WEATHER_TEMP = "#6a513d"  # ramp step 6
WEATHER_RH = "#362b25"  # same ramp, darker step -- one hue, two magnitudes


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
