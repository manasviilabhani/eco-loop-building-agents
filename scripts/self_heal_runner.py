"""Phase 3: autonomous self-healing runner.

Runs EnergyPlus against an IDF. If it fails, tails the .err file and POSTs
it to the agent service's /diagnose endpoint, which lets the LLM call
patch_idf_field itself to produce a corrected variant -- then retries with
the patched file, up to MAX_RETRIES times. No human touches the IDF.

Usage: python scripts/self_heal_runner.py models/broken_bad_people_count.idf
"""

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

EPLUS = "/Applications/EnergyPlus-26-1-0/energyplus"
WEATHER = "/Applications/EnergyPlus-26-1-0/WeatherData/USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"
AGENT_URL = "http://127.0.0.1:8765/diagnose"
MAX_RETRIES = 3
ERR_TAIL_LINES = 40


def run_energyplus(idf_path: Path, out_dir: Path) -> tuple[bool, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [EPLUS, "-w", WEATHER, "-d", str(out_dir), str(idf_path)],
        capture_output=True,
        text=True,
    )
    err_file = out_dir / "eplusout.err"
    err_text = err_file.read_text() if err_file.exists() else proc.stderr
    succeeded = "EnergyPlus Completed Successfully" in (err_text + proc.stdout)
    return succeeded, err_text


def tail_severe_lines(err_text: str, n: int = ERR_TAIL_LINES) -> str:
    lines = err_text.splitlines()
    severe_idxs = [i for i, l in enumerate(lines) if "Severe" in l or "Fatal" in l]
    if not severe_idxs:
        return "\n".join(lines[-n:])
    start = max(0, severe_idxs[0] - 2)
    end = min(len(lines), severe_idxs[-1] + 3)
    return "\n".join(lines[start:end])


def call_diagnose(idf_path: str, err_tail: str, out_path: str) -> dict:
    body = json.dumps({"idf_path": idf_path, "err_log_tail": err_tail, "out_path": out_path}).encode()
    req = urllib.request.Request(AGENT_URL, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read())


def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/self_heal_runner.py <broken_idf_path>")
        sys.exit(1)

    idf_path = Path(sys.argv[1]).resolve()
    run_root = Path(__file__).parent.parent / "runs" / f"self_heal_{idf_path.stem}"

    current_idf = idf_path
    for attempt in range(1, MAX_RETRIES + 2):
        out_dir = run_root / f"attempt_{attempt}"
        print(f"\n=== Attempt {attempt}: running {current_idf.name} ===")
        ok, err_text = run_energyplus(current_idf, out_dir)

        if ok:
            print(f"SUCCESS on attempt {attempt}: {current_idf.name} ran cleanly.")
            return

        if attempt > MAX_RETRIES:
            print(f"FAILED after {MAX_RETRIES} retries. Last error tail:\n{tail_severe_lines(err_text)}")
            sys.exit(1)

        severe_tail = tail_severe_lines(err_text)
        print(f"Run failed. Severe error tail:\n{severe_tail}\n")
        print("Calling agent /diagnose ...")

        patched_path = str(run_root / f"patched_attempt_{attempt}.idf")
        result = call_diagnose(str(current_idf), severe_tail, patched_path)

        if not result.get("ok"):
            print(f"Agent could not produce a patch: {result.get('error')}")
            sys.exit(1)

        print(f"Agent patched the model -> {result['out_path']} (in {result.get('turns')} turn(s))")
        current_idf = Path(result["out_path"])


if __name__ == "__main__":
    main()
