"""serve — the runner side of a warm iPhone session (`guide iphone:run`). ONE Appium Safari session on the booted
simulator lives for the whole session: the keyboard is proven once (preflight), the desk on Alon's machine is told
the session is ready, then the runner long-polls the desk for specs. Each spec gets a clean Safari state, runs with
the harness verbs injected, and its out/ (results.json, shots, safari_console.json, its slice of appium.log) goes
back to the desk as one tar.gz. Exits when the desk says stop or stays unreachable. Prints only counts.
Run as `python -m runner.serve <run_dir>` on the runner; standalone apart from its sibling modules.
"""

import io
import json
import os
import sys
import tarfile
import time
import traceback
from pathlib import Path

import requests

from .forward_proxy import TOKEN_HEADER
from .harness import (APPIUM_URL, Phone, Recorder, RunContext, capabilities, keyboard_preflight,
                      prebuilt_wda_capabilities)

STOP = object()
OK = 200
GONE = 410
FORBIDDEN = 403
NEXT_HOLD_S = 20
HTTP_SLACK_S = 15
CONNECT_TIMEOUT_S = 10
DESK_LOST_AFTER_S = 120
DESK_RETRY_S = 2
LIBRARY_PATH_MARK = "site-packages"


class DeskClient:
    def __init__(self, url: str, *, token: str):
        self.url = url.rstrip("/")
        self.headers = {TOKEN_HEADER: token}
        self.patience = {"lost_after_s": DESK_LOST_AFTER_S, "retry_s": DESK_RETRY_S}

    def with_patience(self, patience: dict) -> "DeskClient":
        self.patience = {**self.patience, **patience}
        return self

    def status_of(self, path: str) -> int:
        return requests.get(self.url + path, headers=self.headers,
                            timeout=(self._connect_timeout(), HTTP_SLACK_S)).status_code

    def _connect_timeout(self) -> float:
        return min(CONNECT_TIMEOUT_S, self.patience["lost_after_s"])

    def _call(self, method: str, request: dict):
        deadline = time.monotonic() + self.patience["lost_after_s"]
        while True:
            try:
                return requests.request(method, self.url + request["path"], headers=self.headers,
                                        timeout=(self._connect_timeout(), request.get("timeout", HTTP_SLACK_S)),
                                        data=request.get("body"))
            except requests.RequestException:
                if time.monotonic() >= deadline:
                    return None
                time.sleep(self.patience["retry_s"])

    def next_job(self):
        answer = self._call("GET", {"path": "/next", "timeout": NEXT_HOLD_S + HTTP_SLACK_S})
        if answer is None or answer.status_code in (GONE, FORBIDDEN):
            return STOP
        return answer.json() if answer.status_code == OK else None

    def post_ready(self, info: dict):
        self._call("POST", {"path": "/ready", "body": json.dumps(info)})

    def post_result(self, job_id: str, bundle: bytes):
        self._call("POST", {"path": f"/result/{job_id}", "body": bundle})


def _appium_offset(serving: dict) -> int:
    log = serving.get("appium_log")
    return log.stat().st_size if log and log.exists() else 0


def _save_appium_slice(serving: dict, *, job: dict):
    log = serving.get("appium_log")
    if log and log.exists():
        with open(log, "rb") as handle:
            handle.seek(job["appium_offset"])
            (job["out_dir"] / "appium.log").write_bytes(handle.read())


def _timed_spec(phone, job: dict) -> dict:
    started = time.monotonic()
    phone.clean_state()
    cleaned = time.monotonic()
    exec(compile(job["spec_source"], job["spec_name"], "exec"), phone.spec_namespace())
    return {"clean_s": round(cleaned - started, 2), "spec_s": round(time.monotonic() - cleaned, 2)}


def _own_traceback(error: BaseException) -> str:
    own_frames = [frame for frame in traceback.extract_tb(error.__traceback__)
                  if LIBRARY_PATH_MARK not in frame.filename]
    error_line = traceback.format_exception_only(error)[0].splitlines()[0]
    return "".join(traceback.format_list(own_frames)) + error_line


def _results(serving: dict, *, job: dict) -> dict:
    phone = serving["phone"]
    recorder = Recorder(out_dir=job["out_dir"], spec=job["spec_name"])
    phone.recorder = recorder
    timings, error = {"clean_s": None, "spec_s": None}, None
    try:
        timings = _timed_spec(phone, job)
    except Exception as failure:
        error = _own_traceback(failure)
    (job["out_dir"] / "safari_console.json").write_text(json.dumps(phone.save_console(), indent=1))
    return {"spec": job["spec_name"], "preflight": serving.get("preflight", {"ok": True}), "expects": recorder.expects,
            "shots": recorder.shots, "tap_retries": recorder.tap_retries, "error": error, "timings": timings}


def run_job(serving: dict, *, job: dict) -> bytes:
    job_dir = Path(serving["run_dir"]) / "jobs" / job["job_id"]
    job = {**job, "out_dir": job_dir / "out", "appium_offset": _appium_offset(serving)}
    (job["out_dir"] / "shots").mkdir(parents=True, exist_ok=True)
    (job["out_dir"] / "results.json").write_text(json.dumps(_results(serving, job=job), indent=1))
    _save_appium_slice(serving, job=job)
    bundle = io.BytesIO()
    with tarfile.open(fileobj=bundle, mode="w:gz") as tar:
        tar.add(job["out_dir"], arcname="out")
    return bundle.getvalue()


def serve_jobs(desk: DeskClient, serving: dict) -> int:
    served = 0
    job = desk.next_job()
    while job is not STOP:
        if job:
            desk.post_result(job["job_id"], run_job(serving, job=job))
            served += 1
            print(f"served spec {served}")
        job = desk.next_job()
    return served


def _driver(start_url: str):
    from appium import webdriver
    from appium.options.common import AppiumOptions
    options = AppiumOptions()
    options.load_capabilities({**capabilities(os.environ["UDID"], start_url),
                               **prebuilt_wda_capabilities(os.environ["WDA_APP"])})
    return webdriver.Remote(APPIUM_URL, options=options)


def _preflight(phone) -> dict:
    try:
        return keyboard_preflight(phone)
    except Exception as failure:
        return {"ok": False, "detail": f"preflight error: {_own_traceback(failure)}"}


def _warm_phone(run: dict, out_dir: Path) -> dict:
    started = time.monotonic()
    driver = _driver(run["base_url"])
    session_s = round(time.monotonic() - started, 1)
    phone = Phone(RunContext(driver=driver, base_url=run["base_url"], recorder=Recorder(out_dir=out_dir)))
    preflight = _preflight(phone)
    timings = {"appium_session_s": session_s, "preflight_s": round(time.monotonic() - started - session_s, 1)}
    return {"phone": phone, "preflight": preflight, "timings": timings}


def main():
    run_dir = Path(sys.argv[1])
    run = json.loads((run_dir / "run.json").read_text())
    desk = DeskClient(run["desk_url"], token=run["token"])
    warm = _warm_phone(run, run_dir / "out")
    served = 0
    try:
        desk.post_ready({"preflight": warm["preflight"], "timings": warm["timings"]})
        if warm["preflight"]["ok"]:
            served = serve_jobs(desk, {**warm, "run_dir": run_dir, "appium_log": run_dir / "out" / "appium.log"})
    finally:
        warm["phone"].driver.quit()
    summary = {"preflight": warm["preflight"], "timings": warm["timings"], "served": served}
    (run_dir / "out" / "session.json").write_text(json.dumps(summary, indent=1))
    print(f"preflight={'ok' if warm['preflight']['ok'] else 'red'} served={served}")


if __name__ == "__main__":
    main()
