"""Build an EnergyPlus .epw weather file from the weather a site really had
on real dates, using the Open-Meteo ERA5 archive (free, no API key).

Why this exists: EnergyPlus ships weather files for five US cities and
nothing else, so there is no way to run the Hyderabad model without
producing one. It also gets us something a stock TMY file cannot -- the
actual hour-by-hour conditions of a specific real week, so the dashboard can
honestly say "this is the week of 19-25 July 2026 in Hyderabad", not "a
statistically typical July".

Two things get written:

  1. <key>_<start>_<end>.epw -- the run-period weather itself.
  2. <key>_design_conditions.json -- heating/cooling design-day conditions
     derived from several years of real history at the same site (see
     `design_conditions()`). EnergyPlus sizes the HVAC equipment from design
     days, and the stock IDF's are Chicago's (-17.3C winter design day),
     which would size the Hyderabad building for a winter that never
     happens. Deriving these from the site's own record avoids both that and
     hand-copying numbers out of an ASHRAE table.

Usage:
    python models/fetch_weather.py --location hyderabad
"""

import argparse
import json
import math
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from models import locations  # noqa: E402

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
# The forecast endpoint serves both the hours already observed today and the
# forecast for the rest of it, which is what a "simulate today" run needs --
# the archive endpoint lags real time by several days and cannot see today at
# all.
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Hourly variables we need to fill an EPW record.
HOURLY_VARS = [
    "temperature_2m",
    "dew_point_2m",
    "relative_humidity_2m",
    "surface_pressure",
    "wind_speed_10m",
    "wind_direction_10m",
    "shortwave_radiation",
    "direct_normal_irradiance",
    "diffuse_radiation",
    "cloud_cover",
    "precipitation",
]

# How many years of history to derive design conditions from.
DESIGN_YEARS = 5

# EPW "missing value" sentinels. EnergyPlus treats each of these as "no data
# for this field" and either derives the value or ignores the field -- which
# is what we want for quantities ERA5 does not provide. Notably, a missing
# horizontal infrared (field 13) makes EnergyPlus compute sky IR from the
# opaque sky cover we DO provide, which is the correct fallback rather than a
# fabricated number.
MISSING = {
    "etr": 9999.0,  # extraterrestrial radiation (unused by EnergyPlus)
    "ir": 9999.0,  # horizontal infrared -> derived from sky cover
    "illum": 999999.0,
    "zenlum": 9999.0,
    "visibility": 9999.0,
    "ceiling": 99999.0,
    "pw_obs": 9,  # 9 = no present-weather observation
    "pw_codes": "999999999",
    "precip_water": 999.0,
    "aerosol": 0.999,
    "snow": 999.0,
    "days_snow": 99,
    "albedo": 999.0,
}


def _fetch(
    lat: float, lon: float, start: str, end: str, tz: str, variables: list[str], *, forecast: bool = False
) -> dict:
    query = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "start_date": start,
            "end_date": end,
            "hourly": ",".join(variables),
            "timezone": tz,
        }
    )
    url = f"{FORECAST_URL if forecast else ARCHIVE_URL}?{query}"
    with urllib.request.urlopen(url, timeout=120) as resp:
        payload = json.load(resp)
    if "hourly" not in payload:
        raise SystemExit(f"Open-Meteo returned no hourly data: {payload}")
    return payload


def _interpolate_gaps(series: list, name: str) -> list:
    """ERA5 is a reanalysis and is normally gap-free, but a null anywhere in
    a weather file makes EnergyPlus either fatal out or silently substitute
    its own default, so fill any hole by linear interpolation between
    neighbours rather than letting it through."""
    values = list(series)
    known = [i for i, v in enumerate(values) if v is not None]
    if not known:
        raise SystemExit(f"weather variable {name!r} came back entirely empty")
    for i, v in enumerate(values):
        if v is not None:
            continue
        before = [k for k in known if k < i]
        after = [k for k in known if k > i]
        if before and after:
            lo, hi = before[-1], after[0]
            frac = (i - lo) / (hi - lo)
            values[i] = values[lo] + frac * (values[hi] - values[lo])
        else:
            values[i] = values[before[-1] if before else after[0]]
    return values


def wet_bulb_c(temp_c: float, rh_pct: float) -> float:
    """Stull (2011) wet-bulb approximation from dry bulb + RH. Accurate to
    about +/-0.3C over the range that matters here, and avoids needing an
    iterative psychrometric solve just to state a design condition."""
    rh = min(max(rh_pct, 5.0), 99.0)  # formula is only valid in this RH band
    return (
        temp_c * math.atan(0.151977 * math.sqrt(rh + 8.313659))
        + math.atan(temp_c + rh)
        - math.atan(rh - 1.676331)
        + 0.00391838 * rh**1.5 * math.atan(0.023101 * rh)
        - 4.686035
    )


def design_conditions(loc: locations.Location) -> dict:
    """Derive ASHRAE-style design conditions from the site's own multi-year
    hourly record.

    Cooling design (1% dry bulb) = the temperature exceeded 1% of all hours;
    heating design (99%) = the temperature exceeded 99% of hours, i.e. the
    cold tail. Coincident wet bulb is the mean wet bulb over the hours near
    that cooling percentile, which is what "MCWB" means and what actually
    drives cooling-coil sizing in a humid climate like Hyderabad -- a
    dry-bulb-only design day would badly undersize the latent capacity here.
    """
    end = date.today() - timedelta(days=7)  # ERA5 archive lags real time
    start = end.replace(year=end.year - DESIGN_YEARS)
    print(f"  fetching {DESIGN_YEARS}y history ({start} -> {end}) for design conditions...")
    payload = _fetch(
        loc.latitude,
        loc.longitude,
        start.isoformat(),
        end.isoformat(),
        loc.tz_name,
        ["temperature_2m", "relative_humidity_2m"],
    )
    hourly = payload["hourly"]
    temps = _interpolate_gaps(hourly["temperature_2m"], "temperature_2m")
    rhs = _interpolate_gaps(hourly["relative_humidity_2m"], "relative_humidity_2m")
    times = hourly["time"]

    ordered = sorted(temps)
    n = len(ordered)
    cooling_db = ordered[int(0.99 * n)]
    heating_db = ordered[int(0.01 * n)]

    # Mean coincident wet bulb: average the wet bulb of hours whose dry bulb
    # sits in the top 2% band around the cooling design point.
    band_lo = ordered[int(0.98 * n)]
    coincident = [wet_bulb_c(t, r) for t, r in zip(temps, rhs) if t >= band_lo]
    mcwb = sum(coincident) / len(coincident)

    # Daily dry-bulb range, averaged over the hottest month -- EnergyPlus
    # uses this to shape the design day's diurnal profile.
    hottest_month = max(
        range(1, 13),
        key=lambda m: sum(t for t, ts in zip(temps, times) if int(ts[5:7]) == m)
        / max(1, sum(1 for ts in times if int(ts[5:7]) == m)),
    )
    per_day: dict[str, list[float]] = {}
    for t, ts in zip(temps, times):
        if int(ts[5:7]) == hottest_month:
            per_day.setdefault(ts[:10], []).append(t)
    ranges = [max(v) - min(v) for v in per_day.values() if len(v) >= 20]
    daily_range = sum(ranges) / len(ranges)

    conditions = {
        "source": f"Open-Meteo ERA5 archive, {start} to {end} ({DESIGN_YEARS}y hourly)",
        "hours_analyzed": n,
        "cooling_design_db_c": round(cooling_db, 1),
        "cooling_design_mcwb_c": round(mcwb, 1),
        "heating_design_db_c": round(heating_db, 1),
        "daily_db_range_c": round(daily_range, 1),
        "hottest_month": hottest_month,
        "annual_mean_db_c": round(sum(temps) / n, 1),
    }
    loc.design_conditions_path.parent.mkdir(parents=True, exist_ok=True)
    loc.design_conditions_path.write_text(json.dumps(conditions, indent=2))
    print(f"  wrote {loc.design_conditions_path}")
    return conditions


def _epw_header(loc: locations.Location, start: date, end: date, ground_temps: list[float]) -> list[str]:
    gt = ",".join(f"{t:.2f}" for t in ground_temps)
    # The data-source field has to tell the truth about which endpoint this
    # came from: a "today" file is part observation and part forecast, and
    # labelling that as ERA5 reanalysis (as this line used to, unconditionally)
    # would misdescribe it to anyone who opened the file later.
    source = (
        "Open-Meteo forecast endpoint (part observed, part FORECAST)"
        if loc.weather_source == "forecast"
        else "Open-Meteo ERA5 reanalysis (observed)"
    )
    return [
        f"LOCATION,{loc.city},{loc.region},{loc.country},{source},"
        f"{loc.wmo},{loc.latitude:.3f},{loc.longitude:.3f},{loc.time_zone},{loc.elevation_m:.1f}",
        "DESIGN CONDITIONS,0",
        "TYPICAL/EXTREME PERIODS,0",
        f"GROUND TEMPERATURES,1,0.5,,,,{gt}",
        "HOLIDAYS/DAYLIGHT SAVINGS,No,0,0,0",
        f"COMMENTS 1,Generated by models/fetch_weather.py from Open-Meteo "
        f"({'forecast endpoint - part observed, part FORECAST' if loc.weather_source == 'forecast' else 'ERA5 archive - observed'}). "
        f"{loc.city}, {start} to {end}; NOT a typical meteorological year.",
        "COMMENTS 2,Sky cover is derived from ERA5 total cloud cover (total and opaque treated as equal). "
        "Horizontal infrared is left missing so EnergyPlus derives it from sky cover. "
        "Rain flags are not modelled; liquid precipitation depth is real.",
        # DATA PERIODS: 1 period, 1 record per hour. The weekday name must be
        # the real weekday of the start date, otherwise EnergyPlus lands the
        # weekday-only occupancy schedule on the wrong days.
        f"DATA PERIODS,1,1,Data,{start.strftime('%A')},{start.month}/{start.day},{end.month}/{end.day}",
    ]


def build_epw(loc: locations.Location) -> Path:
    (start_m, start_d), (end_m, end_d) = loc.run_period
    year = loc.run_year
    start = date(year, start_m, start_d)
    end = date(year, end_m, end_d)

    forecast = loc.weather_source == "forecast"
    kind = "forecast/current" if forecast else "observed"
    print(f"  fetching {kind} weather {start} -> {end} for {loc.city}...")
    payload = _fetch(
        loc.latitude,
        loc.longitude,
        start.isoformat(),
        end.isoformat(),
        loc.tz_name,
        HOURLY_VARS,
        forecast=forecast,
    )
    hourly = payload["hourly"]
    times = hourly["time"]
    data = {v: _interpolate_gaps(hourly[v], v) for v in HOURLY_VARS}

    expected = (end - start).days + 1
    if len(times) != expected * 24:
        raise SystemExit(
            f"expected {expected * 24} hourly records, got {len(times)} -- "
            "the archive may not cover this date range yet"
        )

    # Ground temperature for the EPW header: the standard undisturbed-soil
    # approximation is the site's annual mean dry bulb, damped to near-
    # constant at depth. We only have it if design conditions were derived;
    # fall back to this run's own mean otherwise.
    if loc.design_conditions_path.exists():
        annual_mean = json.loads(loc.design_conditions_path.read_text())["annual_mean_db_c"]
    else:
        annual_mean = sum(data["temperature_2m"]) / len(data["temperature_2m"])
    ground_temps = [annual_mean] * 12

    lines = _epw_header(loc, start, end, ground_temps)

    for i, stamp in enumerate(times):
        d = date(int(stamp[0:4]), int(stamp[5:7]), int(stamp[8:10]))
        clock_hour = int(stamp[11:13])
        # EPW hours run 1..24, where hour H covers the interval ending at H
        # o'clock. Open-Meteo stamps the hour by its start, so 00:00 is the
        # first record of the day -> EPW hour 1.
        epw_hour = clock_hour + 1

        temp = data["temperature_2m"][i]
        dewpt = data["dew_point_2m"][i]
        rh = data["relative_humidity_2m"][i]
        pressure_pa = data["surface_pressure"][i] * 100.0
        wind_ms = data["wind_speed_10m"][i] / 3.6  # km/h -> m/s
        wind_dir = data["wind_direction_10m"][i]
        ghi = data["shortwave_radiation"][i]
        dni = data["direct_normal_irradiance"][i]
        dhi = data["diffuse_radiation"][i]
        sky_tenths = round(data["cloud_cover"][i] / 10.0)
        precip_mm = data["precipitation"][i]

        fields = [
            d.year,
            d.month,
            d.day,
            epw_hour,
            0,  # minute
            "Open-Meteo ERA5",  # data source / uncertainty flags
            f"{temp:.1f}",
            f"{dewpt:.1f}",
            f"{rh:.0f}",
            f"{pressure_pa:.0f}",
            MISSING["etr"],
            MISSING["etr"],
            MISSING["ir"],
            f"{ghi:.0f}",
            f"{dni:.0f}",
            f"{dhi:.0f}",
            MISSING["illum"],
            MISSING["illum"],
            MISSING["illum"],
            MISSING["zenlum"],
            f"{wind_dir:.0f}",
            f"{wind_ms:.1f}",
            sky_tenths,
            sky_tenths,  # opaque sky cover: ERA5 gives one cloud fraction only
            MISSING["visibility"],
            MISSING["ceiling"],
            MISSING["pw_obs"],
            MISSING["pw_codes"],
            MISSING["precip_water"],
            MISSING["aerosol"],
            MISSING["snow"],
            MISSING["days_snow"],
            MISSING["albedo"],
            f"{precip_mm:.1f}",
            1.0,  # liquid precipitation period (hours)
        ]
        lines.append(",".join(str(f) for f in fields))

    loc.weather_file.parent.mkdir(parents=True, exist_ok=True)
    loc.weather_file.write_text("\n".join(lines) + "\n")
    print(f"  wrote {loc.weather_file} ({len(times)} hourly records)")
    return loc.weather_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    locations.add_location_arg(parser)
    parser.add_argument(
        "--skip-design",
        action="store_true",
        help="reuse the existing design-conditions file instead of refetching history",
    )
    args = parser.parse_args()

    loc = locations.get(args.location)
    if loc.weather_source == "tmy":
        raise SystemExit(
            f"{loc.label} uses a bundled TMY weather file "
            f"({loc.weather_file}); nothing to fetch."
        )

    print(f"=== Building weather for {loc.label} ===")
    if args.skip_design and loc.design_conditions_path.exists():
        print(f"  reusing {loc.design_conditions_path}")
        conditions = json.loads(loc.design_conditions_path.read_text())
    else:
        conditions = design_conditions(loc)
    print(json.dumps(conditions, indent=2))
    build_epw(loc)


if __name__ == "__main__":
    main()
