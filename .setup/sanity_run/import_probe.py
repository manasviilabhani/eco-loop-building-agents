import sys
from pyenergyplus.plugin import EnergyPlusPlugin

with open("/tmp/eplus_import_probe.txt", "w") as f:
    f.write("sys.path:\n" + "\n".join(sys.path) + "\n\n")
    try:
        import ollama
        f.write(f"ollama import: OK ({ollama.__file__})\n")
    except Exception as e:
        f.write(f"ollama import: FAILED ({e})\n")
    try:
        import mcp
        f.write(f"mcp import: OK ({mcp.__file__})\n")
    except Exception as e:
        f.write(f"mcp import: FAILED ({e})\n")


class ImportProbe(EnergyPlusPlugin):
    def on_begin_new_environment(self, state) -> int:
        return 0
