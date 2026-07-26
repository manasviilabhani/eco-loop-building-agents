"""Phase 1: derive our baseline.idf from EnergyPlus's bundled 5ZoneAirCooled.idf.

Changes made to the stock example file:
  - RunPeriod trimmed to a representative summer week (July 1-7, Chicago) so
    each simulation run takes seconds, not minutes -- needed since we run
    this many times (baseline, AI closed-loop, broken-variant self-heal
    tests) within a hackathon time budget.
  - Adds a Fanger comfort model to each zone's EXISTING People object (the
    stock file already defines one People object per zone, e.g. "SPACE1-1
    People 1", wired to a real OCCUPY-1/ActSchd occupancy pattern -- an
    earlier version of this script mistakenly created a second, duplicate
    People object per zone instead of extending the real one, which silently
    double-counted internal heat gains AND made the self-healing demo flaky
    (two "SPACE2-1 People*" objects made object-name matching ambiguous).
    PMV output requires a Fanger comfort model, which is a required brief
    metric ("Thermal Comfort & Constraints", 20% of grading) but wasn't on
    the stock People objects.
  - Adds Output:Variable requests for PMV and zone temps at Timestep
    resolution (stock file only requests hourly for some variables), plus a
    proper Output:Meter for Electricity:Facility (the stock file only had
    Output:Meter:MeterFileOnly, which writes to eplusout.mtr and is silently
    skipped by ReadVarsESO / never reaches eplusout.csv).

Thermostat setpoint schedules confirmed by inspection: "Clg-SetP-Sch" and
"Htg-SetP-Sch" (Schedule:Compact objects), shared across all 5 zones via
ThermostatSetpoint:SingleCooling/SingleHeating "CoolingSetpoint"/
"HeatingSetpoint". These are exactly what plugin/eco_loop_plugin.py will
actuate via the Schedule:Compact / Schedule Value actuator pattern.

The site is a parameter (see models/locations.py). With --location chicago
(the default) this reproduces the original Chicago baseline.idf byte-for-
byte in intent; with --location hyderabad it re-sites the same building in
India, which additionally requires rewriting Site:Location and the design
days -- see `apply_location()`.

Usage: python models/prepare_baseline.py [--location chicago|hyderabad]
"""

import argparse
import json
import sys
from pathlib import Path

from eppy.modeleditor import IDF

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from models import locations  # noqa: E402

EPLUS_DIR = Path("/Applications/EnergyPlus-26-1-0")
IDD_PATH = EPLUS_DIR / "Energy+.idd"
SOURCE_IDF = EPLUS_DIR / "ExampleFiles" / "5ZoneAirCooled.idf"

ZONES = ["SPACE1-1", "SPACE2-1", "SPACE3-1", "SPACE4-1", "SPACE5-1"]


def apply_location(idf, loc: locations.Location):
    """Re-site the building: run period, Site:Location, and design days.

    For the default TMY location this only trims the run period -- the stock
    file is already a Chicago model, so its Site:Location and design days are
    already correct and are left untouched.

    For an observed-weather location the other two matter:

      - Site:Location must match the .epw's own header. EnergyPlus warns and
        then uses the weather file's values if they disagree, but latitude
        and time zone drive the solar position calculation, so a stale
        Chicago header here would put the sun in the wrong place all week.

      - SizingPeriod:DesignDay objects are what EnergyPlus autosizes the HVAC
        equipment from, and they are NOT read from the weather file. Left
        alone, the Hyderabad building would be sized for Chicago's -17.3C
        winter design day and 31.5C summer day: massively oversized heating,
        undersized cooling, and a latent capacity set for a dry climate
        rather than a monsoon one. We replace them with design conditions
        derived from Hyderabad's own multi-year record (models/fetch_weather.py).
    """
    (start_m, start_d), (end_m, end_d) = loc.run_period

    rp = idf.idfobjects["RUNPERIOD"][0]
    rp.Begin_Month, rp.Begin_Day_of_Month = start_m, start_d
    rp.End_Month, rp.End_Day_of_Month = end_m, end_d

    if loc.weather_source == "tmy":
        return

    # Pin the real calendar year and clear the hardcoded start weekday, so
    # EnergyPlus derives the true weekday for these dates. The occupancy
    # schedule (OCCUPY-1) is weekday-only, so getting this wrong would shift
    # the whole week's load pattern by a day or more.
    rp.Begin_Year, rp.End_Year = loc.run_year, loc.run_year
    rp.Day_of_Week_for_Start_Day = ""

    site = idf.idfobjects["SITE:LOCATION"][0]
    site.Name = f"{loc.city.upper()}_{loc.region.replace(' ', '.')}_{loc.country} Open-Meteo"
    site.Latitude = loc.latitude
    site.Longitude = loc.longitude
    site.Time_Zone = loc.time_zone
    site.Elevation = loc.elevation_m

    if not loc.design_conditions_path.exists():
        raise SystemExit(
            f"Missing {loc.design_conditions_path}.\n"
            f"Run: python models/fetch_weather.py --location {loc.key}"
        )
    dc = json.loads(loc.design_conditions_path.read_text())

    # Replace, don't append -- leaving Chicago's design days in place would
    # keep sizing off the colder/drier of the two sites.
    for obj in list(idf.idfobjects["SIZINGPERIOD:DESIGNDAY"]):
        idf.removeidfobject(obj)

    # Barometric pressure at the site elevation (standard atmosphere), which
    # design days carry explicitly. Hyderabad sits ~505m up, so using the
    # stock sea-level-ish Chicago value would misstate air density and thus
    # the sizing airflow.
    pressure_pa = round(101325.0 * (1 - 2.25577e-5 * loc.elevation_m) ** 5.25588)

    idf.newidfobject(
        "SIZINGPERIOD:DESIGNDAY",
        Name=f"{loc.city} Annual Heating 99% Design Conditions DB",
        Month=1,
        Day_of_Month=21,
        Day_Type="WinterDesignDay",
        Maximum_DryBulb_Temperature=dc["heating_design_db_c"],
        Daily_DryBulb_Temperature_Range=0.0,
        Humidity_Condition_Type="Wetbulb",
        Wetbulb_or_DewPoint_at_Maximum_DryBulb=dc["heating_design_db_c"],
        Barometric_Pressure=pressure_pa,
        Wind_Speed=2.5,
        Wind_Direction=0,
        Rain_Indicator="No",
        Snow_Indicator="No",
        Daylight_Saving_Time_Indicator="No",
        Solar_Model_Indicator="ASHRAEClearSky",
        Sky_Clearness=0.0,
    )
    idf.newidfobject(
        "SIZINGPERIOD:DESIGNDAY",
        Name=f"{loc.city} Annual Cooling 1% Design Conditions DB/MCWB",
        Month=dc["hottest_month"],
        Day_of_Month=21,
        Day_Type="SummerDesignDay",
        Maximum_DryBulb_Temperature=dc["cooling_design_db_c"],
        Daily_DryBulb_Temperature_Range=dc["daily_db_range_c"],
        Humidity_Condition_Type="Wetbulb",
        Wetbulb_or_DewPoint_at_Maximum_DryBulb=dc["cooling_design_mcwb_c"],
        Barometric_Pressure=pressure_pa,
        Wind_Speed=2.5,
        Wind_Direction=0,
        Rain_Indicator="No",
        Snow_Indicator="No",
        Daylight_Saving_Time_Indicator="No",
        Solar_Model_Indicator="ASHRAEClearSky",
        Sky_Clearness=1.0,
    )


def main():
    parser = argparse.ArgumentParser(description="Build baseline.idf for a site.")
    locations.add_location_arg(parser)
    args = parser.parse_args()
    loc = locations.get(args.location)
    out_idf = loc.baseline_idf

    IDF.setiddname(str(IDD_PATH))
    idf = IDF(str(SOURCE_IDF))

    apply_location(idf, loc)

    # --- Shared schedules for occupancy/comfort inputs ---
    # NOTE: "Fraction" ScheduleTypeLimits already exists in the source file --
    # do not redefine it (EnergyPlus treats a second definition as fatal, not
    # a harmless override).
    # NOTE: reuse the source file's own "OCCUPY-1" (weekday 8am-7pm
    # occupancy fraction) and "ActSchd" (117.24 W activity level) schedules
    # instead of inventing an "always occupied" one -- an earlier version of
    # this script used a constant-1.0 occupancy schedule, which meant PMV
    # comfort was being enforced 24/7, including empty overnight hours when
    # the building's own setpoint schedule (Clg-SetP-Sch) intentionally lets
    # temperature drift wide (29.4C setback) to save energy. That mismatch
    # made the baseline's PMV violation rate look artificially bad and gave
    # the agent an incentive to fight the setback schedule at night for no
    # real comfort benefit (nobody's there) -- it made both energy and
    # comfort worse. Tying comfort tracking to real occupancy fixes both.
    idf.newidfobject(
        "SCHEDULE:CONSTANT",
        Name="Office Work Efficiency",
        Schedule_Type_Limits_Name="",
        Hourly_Value=0.0,  # no external mechanical work being done, standard office assumption
    )
    idf.newidfobject(
        "SCHEDULE:CONSTANT",
        Name="Office Clothing Insulation",
        Schedule_Type_Limits_Name="",
        Hourly_Value=0.6,  # clo, typical business casual
    )
    idf.newidfobject(
        "SCHEDULE:CONSTANT",
        Name="Office Air Velocity",
        Schedule_Type_Limits_Name="",
        Hourly_Value=0.1,  # m/s, typical still indoor air
    )

    # --- Add Fanger comfort model to each zone's EXISTING People object ---
    # (named "{zone} People 1" in the stock file, already wired to OCCUPY-1 /
    # ActSchd -- extend it in place rather than adding a second object).
    for zone in ZONES:
        people = next(
            o for o in idf.idfobjects["PEOPLE"]
            if str(o.Name).casefold() == f"{zone} People 1".casefold()
        )
        people.Work_Efficiency_Schedule_Name = "Office Work Efficiency"
        people.Clothing_Insulation_Calculation_Method = "ClothingInsulationSchedule"
        people.Clothing_Insulation_Schedule_Name = "Office Clothing Insulation"
        people.Air_Velocity_Schedule_Name = "Office Air Velocity"
        people.Thermal_Comfort_Model_1_Type = "Fanger"

    # --- Output requests needed for the dashboard / agent telemetry ---
    idf.newidfobject(
        "OUTPUT:VARIABLE",
        Key_Value="*",
        Variable_Name="Zone Thermal Comfort Fanger Model PMV",
        Reporting_Frequency="Timestep",
    )
    idf.newidfobject(
        "OUTPUT:VARIABLE",
        Key_Value="*",
        Variable_Name="Zone Mean Air Temperature",
        Reporting_Frequency="Timestep",
    )
    idf.newidfobject(
        "OUTPUT:VARIABLE",
        Key_Value="*",
        Variable_Name="Facility Total Electricity Demand Rate",
        Reporting_Frequency="Timestep",
    )
    idf.newidfobject(
        "OUTPUT:METER",
        Key_Name="Electricity:Facility",
        Reporting_Frequency="Timestep",
    )
    # Outdoor conditions -- the actual driver behind every other series, and
    # the only way the dashboard can show *why* two sites behave differently
    # rather than just that they do. Relative humidity matters as much as
    # drybulb once the model is sited somewhere monsoonal, where latent load
    # rather than peak temperature is what the cooling system is fighting.
    idf.newidfobject(
        "OUTPUT:VARIABLE",
        Key_Value="Environment",
        Variable_Name="Site Outdoor Air Drybulb Temperature",
        Reporting_Frequency="Timestep",
    )
    idf.newidfobject(
        "OUTPUT:VARIABLE",
        Key_Value="Environment",
        Variable_Name="Site Outdoor Air Relative Humidity",
        Reporting_Frequency="Timestep",
    )
    # Needed so the closed-loop plugin can read the current setpoint via
    # get_variable_handle -- schedule value sensors only resolve if an
    # Output:Variable request for that exact key exists somewhere in the IDF.
    idf.newidfobject(
        "OUTPUT:VARIABLE",
        Key_Value="Clg-SetP-Sch",
        Variable_Name="Schedule Value",
        Reporting_Frequency="Timestep",
    )
    idf.newidfobject(
        "OUTPUT:VARIABLE",
        Key_Value="Htg-SetP-Sch",
        Variable_Name="Schedule Value",
        Reporting_Frequency="Timestep",
    )
    # Needed to filter PMV comfort violations to actually-occupied hours --
    # EnergyPlus computes PMV from the (constant) clothing/activity schedules
    # regardless of occupancy fraction, so an empty zone still gets a PMV
    # value; comfort is only meaningful when someone is there to feel it.
    idf.newidfobject(
        "OUTPUT:VARIABLE",
        Key_Value="OCCUPY-1",
        Variable_Name="Schedule Value",
        Reporting_Frequency="Timestep",
    )

    idf.saveas(str(out_idf))
    print(f"Wrote {out_idf}  [{loc.label}]")


if __name__ == "__main__":
    main()
