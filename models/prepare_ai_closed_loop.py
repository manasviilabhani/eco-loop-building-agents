"""Phase 2: derive ai_closed_loop.idf from baseline.idf, wiring in the
EcoLoopController Python Plugin (plugin/eco_loop_plugin.py) that talks to
the live agent service (agent/service.py, must be running on :8765) and
actuates the Clg-SetP-Sch / Htg-SetP-Sch schedules mid-simulation.

Same building, same trimmed run period as baseline.idf -- the only
difference is this plugin wiring, so the baseline-vs-AI comparison isolates
the effect of the agent's control decisions.
"""

import argparse
import sys
from pathlib import Path

from eppy.modeleditor import IDF

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from models import locations  # noqa: E402

EPLUS_DIR = Path("/Applications/EnergyPlus-26-1-0")
PLUGIN_DIR = Path(__file__).parent.parent / "plugin"


def build(loc: locations.Location):
    """Importable entry point -- see prepare_baseline.build()."""
    if not loc.baseline_idf.exists():
        raise SystemExit(
            f"Missing {loc.baseline_idf}.\n"
            f"Run: python models/prepare_baseline.py --location {loc.key}"
        )

    IDF.setiddname(str(EPLUS_DIR / "Energy+.idd"))
    idf = IDF(str(loc.baseline_idf))

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

    idf.saveas(str(loc.ai_idf))
    print(f"Wrote {loc.ai_idf}  [{loc.label}]")
    return loc.ai_idf


def main():
    parser = argparse.ArgumentParser(description="Build ai_closed_loop.idf for a site.")
    locations.add_location_arg(parser)
    args = parser.parse_args()
    build(locations.get(args.location))


if __name__ == "__main__":
    main()
