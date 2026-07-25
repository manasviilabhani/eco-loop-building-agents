"""Phase 3: seed deliberately broken IDF variants from baseline.idf, each
with one realistic, targeted fault, to demonstrate the agent's autonomous
self-healing loop (scripts/self_heal_runner.py): a crashed run gets
diagnosed from its own .err log and patched by the LLM itself, without a
human touching the file.
"""

from pathlib import Path
from eppy.modeleditor import IDF

EPLUS_DIR = Path("/Applications/EnergyPlus-26-1-0")
BASELINE_IDF = Path(__file__).parent / "baseline.idf"
OUT_DIR = Path(__file__).parent


def make_bad_people_count():
    """Fault: negative occupant count on SPACE1-1 People object."""
    IDF.setiddname(str(EPLUS_DIR / "Energy+.idd"))
    idf = IDF(str(BASELINE_IDF))
    people = next(o for o in idf.idfobjects["PEOPLE"] if o.Name == "SPACE1-1 People")
    people.Number_of_People = -5
    idf.saveas(str(OUT_DIR / "broken_bad_people_count.idf"))


def make_dangling_schedule_ref():
    """Fault: People object references a schedule that doesn't exist."""
    IDF.setiddname(str(EPLUS_DIR / "Energy+.idd"))
    idf = IDF(str(BASELINE_IDF))
    people = next(o for o in idf.idfobjects["PEOPLE"] if o.Name == "SPACE2-1 People")
    people.Activity_Level_Schedule_Name = "Nonexistent Activity Schedule"
    idf.saveas(str(OUT_DIR / "broken_dangling_schedule.idf"))


def make_invalid_comfort_model():
    """Fault: invalid enum value for the Fanger comfort model field."""
    IDF.setiddname(str(EPLUS_DIR / "Energy+.idd"))
    idf = IDF(str(BASELINE_IDF))
    people = next(o for o in idf.idfobjects["PEOPLE"] if o.Name == "SPACE3-1 People")
    people.Thermal_Comfort_Model_1_Type = "Fangerz"
    idf.saveas(str(OUT_DIR / "broken_invalid_comfort_model.idf"))


if __name__ == "__main__":
    make_bad_people_count()
    make_dangling_schedule_ref()
    make_invalid_comfort_model()
    print("Wrote 3 broken IDF variants")
