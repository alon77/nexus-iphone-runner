"""settle — booted is not ready (chain iphone_boot_fix_20261009). Session 20261009_003602: bootstatus said booted
while iOS first-boot setup and diagnosticd drove the Mac's load 74 -> 758 for the whole session. The session is
declared ready only once the Mac's CPU sits idle: SETTLED_SAMPLES samples in a row at SETTLED_IDLE_PCT idle or more,
each sample printed with its second. Never settled within SETTLE_LIMIT_S -> exit 1 naming the busiest processes.
Run as `python -m runner.settle <run_dir>` on the runner; standalone. The verdict lands in out/boot_steps.txt.
"""

import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SETTLED_IDLE_PCT = 70.0
SETTLED_SAMPLES = 3
SETTLE_LIMIT_S = 600
BUSIEST_SHOWN = 5
TOP_SAMPLE = ["top", "-l", "2", "-s", "1", "-n", str(BUSIEST_SHOWN), "-o", "cpu", "-stats", "command,cpu"]
IDLE_LINE = re.compile(r"CPU usage:.*?([\d.]+)% idle")
TABLE_HEADER = re.compile(r"^COMMAND\s+%CPU\s*$", re.M)
BOOT_STEPS = "boot_steps.txt"


def idle_pct(top_output: str) -> float:
    return float(IDLE_LINE.findall(top_output)[-1])


def busiest(top_output: str) -> list:
    last_table = TABLE_HEADER.split(top_output)[-1]
    return [" ".join(line.split()) for line in last_table.splitlines() if line.strip()][:BUSIEST_SHOWN]


def sample() -> str:
    return subprocess.run(TOP_SAMPLE, capture_output=True, text=True, check=True).stdout


@dataclass
class Watch:
    sampler: Callable[[], str] = sample
    clock: Callable[[], float] = time.monotonic
    limit_s: float = SETTLE_LIMIT_S


def wait_settled(watch: Watch) -> dict:
    started = watch.clock()
    in_a_row = 0
    while True:
        top_output = watch.sampler()
        idle, waited = idle_pct(top_output), watch.clock() - started
        in_a_row = in_a_row + 1 if idle >= SETTLED_IDLE_PCT else 0
        print(f"{waited:5.0f}s idle {idle:.0f}%", flush=True)
        if in_a_row >= SETTLED_SAMPLES:
            return {"settled": True, "after_s": round(waited, 1)}
        if waited >= watch.limit_s:
            return {"settled": False, "idle_pct": idle, "busiest": busiest(top_output)}


def verdict_line(verdict: dict) -> str:
    if verdict["settled"]:
        return f"phone settled after {verdict['after_s']}s"
    return (f"the phone never settled: {verdict['idle_pct']:.0f}% idle after {SETTLE_LIMIT_S}s, busiest: "
            + ", ".join(verdict["busiest"]))


def main():
    verdict = wait_settled(Watch())
    line = f"{time.strftime('%H:%M:%S')} {verdict_line(verdict)}"
    print(line)
    with open(Path(sys.argv[1]) / "out" / BOOT_STEPS, "a") as steps:
        steps.write(line + "\n")
    if not verdict["settled"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
