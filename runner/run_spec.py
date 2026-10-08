"""run_spec — opens an Appium Safari session on the booted simulator (UDID from the env), runs the keyboard
preflight, then the spec with the harness verbs injected, and writes out/results.json + out/safari_console.json.
Prints only counts: details stay in the sealed bundle. `guide iphone:run`.
"""

import json
import os
import sys
import traceback
from pathlib import Path

from harness import (APPIUM_URL, Phone, Recorder, RunContext, capabilities, keyboard_preflight,
                     prebuilt_wda_capabilities)

TRACEBACK_FRAMES = 6


def _driver(start_url: str):
    from appium import webdriver
    from appium.options.common import AppiumOptions
    options = AppiumOptions()
    options.load_capabilities({**capabilities(os.environ["UDID"], start_url),
                               **prebuilt_wda_capabilities(os.environ["WDA_APP"])})
    return webdriver.Remote(APPIUM_URL, options=options)


def _save_console(driver, out_dir: Path):
    try:
        entries = driver.get_log("safariConsole")
    except Exception as error:
        entries = [{"level": "nexus", "message": f"console unavailable: {type(error).__name__}"}]
    (out_dir / "safari_console.json").write_text(json.dumps(entries, indent=1))


def _run(run_dir: Path, recorder: Recorder):
    run = json.loads((run_dir / "run.json").read_text())
    recorder.spec = run["spec_name"]
    driver = _driver(run["base_url"])
    try:
        phone = Phone(RunContext(driver=driver, base_url=run["base_url"], recorder=recorder))
        recorder.preflight = keyboard_preflight(phone)
        if recorder.preflight["ok"]:
            source = (run_dir / "spec.py").read_text()
            exec(compile(source, run["spec_name"], "exec"), phone.spec_namespace())
    finally:
        _save_console(driver, recorder.out_dir)
        driver.quit()


def main():
    run_dir = Path(sys.argv[1])
    recorder = Recorder(out_dir=run_dir / "out")
    error = None
    try:
        _run(run_dir, recorder)
    except Exception:
        error = traceback.format_exc(limit=-TRACEBACK_FRAMES)
    results = {"spec": recorder.spec, "preflight": recorder.preflight, "expects": recorder.expects,
               "shots": recorder.shots, "error": error}
    (recorder.out_dir / "results.json").write_text(json.dumps(results, indent=1))
    reds = sum(not expect["ok"] for expect in recorder.expects)
    print(f"preflight={'ok' if (recorder.preflight or {}).get('ok') else 'red'} expects={len(recorder.expects)} "
          f"red={reds} error={'yes' if error else 'no'}")


if __name__ == "__main__":
    main()
