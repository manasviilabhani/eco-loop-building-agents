"""Phase 1: derive our baseline.idf from EnergyPlus's bundled 5ZoneAirCooled.idf.

Changes made to the stock example file:
  - RunPeriod trimmed to a representative summer week (July 1-7, Chicago) so
    each simulation run takes seconds, not minutes -- needed since we run
    this many times (baseline, AI closed-loop, broken-variant self-heal
    tests) within a hackathon time budget.
  - Adds People objects (one per zone, Fanger comfort model) since the stock
    file has zero occupants and therefore no PMV output at all -- PMV is a
    required brief metric ("Thermal Comfort & Constraints", 20% of grading).
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
"""

from pathlib import Path
from eppy.modeleditor import IDF

EPLUS_DIR = Path("/Applications/EnergyPlus-26-1-0")
IDD_PATH = EPLUS_DIR / "Energy+.idd"
SOURCE_IDF = EPLUS_DIR / "ExampleFiles" / "5ZoneAirCooled.idf"
OUT_IDF = Path(__file__).parent / "baseline.idf"

ZONES = ["SPACE1-1", "SPACE2-1", "SPACE3-1", "SPACE4-1", "SPACE5-1"]

RUN_PERIOD_START = (7, 1)  # July 1 -- Chicago summer week, cooling-dominated
RUN_PERIOD_END = (7, 7)


def main():
    IDF.setiddname(str(IDD_PATH))
    idf = IDF(str(SOURCE_IDF))

    # --- Trim RunPeriod ---
    rp = idf.idfobjects["RUNPERIOD"][0]
    rp.Begin_Month, rp.Begin_Day_of_Month = RUN_PERIOD_START
    rp.End_Month, rp.End_Day_of_Month = RUN_PERIOD_END

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

    # --- People + Fanger comfort model, one per zone ---
    for zone in ZONES:
        idf.newidfobject(
            "PEOPLE",
            Name=f"{zone} People",
            Zone_or_ZoneList_or_Space_or_SpaceList_Name=zone,
            Number_of_People_Schedule_Name="OCCUPY-1",
            Number_of_People_Calculation_Method="People",
            Number_of_People=5,
            Fraction_Radiant=0.3,
            Activity_Level_Schedule_Name="ActSchd",
            Work_Efficiency_Schedule_Name="Office Work Efficiency",
            Clothing_Insulation_Calculation_Method="ClothingInsulationSchedule",
            Clothing_Insulation_Schedule_Name="Office Clothing Insulation",
            Air_Velocity_Schedule_Name="Office Air Velocity",
            Thermal_Comfort_Model_1_Type="Fanger",
        )

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

    idf.saveas(str(OUT_IDF))
    print(f"Wrote {OUT_IDF}")


if __name__ == "__main__":
    main()
