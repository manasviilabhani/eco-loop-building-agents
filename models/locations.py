"""Location registry -- makes the site a parameter instead of a hardcoded
Chicago assumption.

Every script that touches a building model or a run (prepare_baseline.py,
prepare_ai_closed_loop.py, scripts/run_comparison.py, dashboard/app.py)
resolves its paths through a Location here, so adding a city means adding an
entry, not editing five files.

Two kinds of weather source are supported:

  - "tmy": a Typical Meteorological Year file that ships with EnergyPlus.
    Chicago uses this. A TMY is a synthetic "typical" year stitched from
    many real years, so it has no meaningful calendar year -- run_year is
    None and the run period is just a month/day window.

  - "observed": a real, hour-by-hour record of what the weather actually
    did at that site on specific real dates, fetched from the Open-Meteo
    ERA5 archive and written out as an .epw by models/fetch_weather.py.
    Hyderabad uses this. run_year is the real year, so EnergyPlus lands the
    run on the correct real weekdays (which matters -- the building's
    occupancy schedule is weekday-only).

EnergyPlus ships no Indian weather file, which is why Hyderabad needs the
fetch step; see models/fetch_weather.py.

Filename convention: the default location (Chicago) uses unsuffixed names
(baseline.idf, comparison_summary.json) so every pre-existing path, the
committed Chicago results, and the self-healing demo keep working untouched.
Other locations get a "_{key}" suffix.
"""

from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path

MODELS_DIR = Path(__file__).parent
REPO = MODELS_DIR.parent
WEATHER_DIR = MODELS_DIR / "weather"

EPLUS_DIR = Path("/Applications/EnergyPlus-26-1-0")


@dataclass(frozen=True)
class Location:
    key: str
    label: str  # shown in the dashboard
    city: str
    region: str
    country: str  # 3-letter EPW country code
    wmo: str
    latitude: float
    longitude: float
    time_zone: float  # hours offset from UTC (India is +5.5, hence float)
    elevation_m: float
    tz_name: str  # IANA name, used when asking Open-Meteo for local time
    weather_source: str  # "tmy" | "observed"
    run_period: tuple[tuple[int, int], tuple[int, int]]  # ((mon, day), (mon, day))
    run_year: int | None  # real year for "observed"; None for TMY
    weather_file: Path
    is_default: bool = False
    # A "live" variant reuses its parent site's design conditions rather than
    # deriving its own -- design days describe the *climate*, which does not
    # change because we are simulating a different day of it.
    design_source_key: str | None = None

    @property
    def suffix(self) -> str:
        return "" if self.is_default else f"_{self.key}"

    @property
    def baseline_idf(self) -> Path:
        return MODELS_DIR / f"baseline{self.suffix}.idf"

    @property
    def ai_idf(self) -> Path:
        return MODELS_DIR / f"ai_closed_loop{self.suffix}.idf"

    @property
    def summary_path(self) -> Path:
        return REPO / "runs" / f"comparison_summary{self.suffix}.json"

    @property
    def design_conditions_path(self) -> Path:
        """Design-day conditions derived from real multi-year history for
        this site (written by fetch_weather.py). Only "observed" locations
        need this -- TMY locations already carry design days in the stock
        IDF."""
        return WEATHER_DIR / f"{self.design_source_key or self.key}_design_conditions.json"

    def run_dir(self, which: str) -> Path:
        return REPO / "runs" / f"{which}_full{self.suffix}"


LOCATIONS: dict[str, Location] = {
    "chicago": Location(
        key="chicago",
        label="Chicago, IL (USA)",
        city="Chicago",
        region="Illinois",
        country="USA",
        wmo="725300",
        latitude=41.78,
        longitude=-87.75,
        time_zone=-6.0,
        elevation_m=190.0,
        tz_name="America/Chicago",
        weather_source="tmy",
        run_period=((7, 1), (7, 7)),
        run_year=None,
        weather_file=EPLUS_DIR / "WeatherData" / "USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw",
        is_default=True,
    ),
    "hyderabad": Location(
        key="hyderabad",
        label="Hyderabad, Telangana (India)",
        city="Hyderabad",
        region="Telangana",
        country="IND",
        # Rajiv Gandhi International Airport, the WMO station Indian building
        # weather files are normally keyed to.
        wmo="431280",
        latitude=17.385,
        longitude=78.4867,
        time_zone=5.5,
        elevation_m=505.0,  # confirmed against Open-Meteo's terrain model for this cell
        tz_name="Asia/Kolkata",
        weather_source="observed",
        # A real, recent week: 19-25 July 2026. This is peak south-west
        # monsoon in Hyderabad -- humid and heavily overcast rather than
        # blazing hot, which is a genuinely different control problem from
        # Chicago's dry summer week (latent load dominates, and cloud cover
        # suppresses the solar gain the agent would otherwise be reacting to).
        run_period=((7, 19), (7, 25)),
        run_year=2026,
        weather_file=WEATHER_DIR / "hyderabad_2026-07-19_2026-07-25.epw",
    ),
}

DEFAULT_LOCATION = "chicago"

# --- Live "today" variants -------------------------------------------------
#
# A live site simulates *today* at a real location, on weather pulled fresh
# from Open-Meteo's forecast endpoint (which serves both the hours already
# observed today and the forecast for the rest of it). scripts/live_daemon.py
# rebuilds this every cycle so the day being simulated is always the current
# one.
#
# The entry registered in LOCATIONS is a placeholder whose run period is
# resolved at import time; only its key and label are used by the dashboards.
# The daemon always calls live_variant() to get a Location pinned to the real
# current date, so a process running past midnight still rolls over correctly.

LIVE_SUFFIX = "_live"


def live_variant(base_key: str, day: date | None = None, days: int = 1) -> Location:
    """Build a Location for `days` starting at `day` (default: today) at the
    same physical site as `base_key`, sourced from forecast rather than
    archive weather."""
    base = LOCATIONS[base_key] if base_key in LOCATIONS else get(base_key)
    day = day or date.today()
    end = day + timedelta(days=days - 1)
    return replace(
        base,
        key=f"{base_key}{LIVE_SUFFIX}",
        label=f"{base.city} — live (today)",
        weather_source="forecast",
        run_period=((day.month, day.day), (end.month, end.day)),
        run_year=day.year,
        weather_file=WEATHER_DIR / f"{base_key}{LIVE_SUFFIX}.epw",
        is_default=False,
        design_source_key=base_key,
    )


# Register a live variant for every base site, not just one. These have to be
# in LOCATIONS for the live view to resolve them: it identifies a run's site by
# matching the run_id's prefix against the registered keys, so an unregistered
# "chicago_live-..." run would fail every match and silently fall back to the
# default site -- i.e. a Chicago live run would be plotted as if it were the
# plain Chicago week.
for _base in [k for k, v in LOCATIONS.items() if not k.endswith(LIVE_SUFFIX)]:
    LOCATIONS[f"{_base}{LIVE_SUFFIX}"] = live_variant(_base)


def get(key: str) -> Location:
    try:
        return LOCATIONS[key]
    except KeyError:
        raise SystemExit(
            f"Unknown location {key!r}. Available: {', '.join(LOCATIONS)}"
        ) from None


def add_location_arg(parser):
    """Shared --location flag so every entry point spells it the same way."""
    parser.add_argument(
        "--location",
        default=DEFAULT_LOCATION,
        choices=sorted(LOCATIONS),
        help=f"site to build/run for (default: {DEFAULT_LOCATION})",
    )
