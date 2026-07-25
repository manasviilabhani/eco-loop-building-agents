"""Phase 2: derive ai_closed_loop.idf from baseline.idf, wiring in the
EcoLoopController Python Plugin (plugin/eco_loop_plugin.py) that talks to
the live agent service (agent/service.py, must be running on :8765) and
actuates the Clg-SetP-Sch / Htg-SetP-Sch schedules mid-simulation.

Same building, same trimmed run period as baseline.idf -- the only
difference is this plugin wiring, so the baseline-vs-AI comparison isolates
the effect of the agent's control decisions.
"""

from pathlib import Path
from eppy.modeleditor import IDF

EPLUS_DIR = Path("/Applications/EnergyPlus-26-1-0")
BASELINE_IDF = Path(__file__).parent / "baseline.idf"
OUT_IDF = Path(__file__).parent / "ai_closed_loop.idf"
PLUGIN_DIR = Path(__file__).parent.parent / "plugin"


def main():
    IDF.setiddname(str(EPLUS_DIR / "Energy+.idd"))
    idf = IDF(str(BASELINE_IDF))

    idf.newidfobject(
        "PYTHONPLUGIN:SEARCHPATHS",
        Name="Eco Loop Search Paths",
        Add_Current_Working_Directory_to_Search_Path="Yes",
        Add_Input_File_Directory_to_Search_Path="Yes",
        Add_epin_Environment_Variable_to_Search_Path="Yes",
        Search_Path_1=str(PLUGIN_DIR),
    )
    idf.newidfobject(
        "PYTHONPLUGIN:INSTANCE",
        Name="Eco Loop Controller Instance",
        Run_During_Warmup_Days="No",
        Python_Module_Name="eco_loop_plugin",
        Plugin_Class_Name="EcoLoopController",
    )

    idf.saveas(str(OUT_IDF))
    print(f"Wrote {OUT_IDF}")


if __name__ == "__main__":
    main()
