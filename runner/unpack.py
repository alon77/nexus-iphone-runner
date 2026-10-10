"""unpack — the runner's first step: open the sealed dispatch payload, mask every value in the job log, lay out the
run dir (run.json, hosts_lines, out/) and set the job's DEVICE_TYPE (the payload's device, else DEFAULT_DEVICE_TYPE)
for every later step. Reads PAYLOAD and IPHONE_RUN_KEY from the env, never from argv, so neither reaches the job log.
`guide iphone:tunnel`.
"""

import json
import os
import re
import sys
from pathlib import Path

from sealed import unseal

OWNER_ONLY = 0o600
DEVICE_TYPE_SHAPE = re.compile(r"com\.apple\.CoreSimulator\.SimDeviceType\.[A-Za-z0-9-]+")


def _mask(values):
    for value in values:
        for line in str(value).splitlines():
            if line.strip():
                print(f"::add-mask::{line}")


def _secrets_of(payload: dict):
    yield payload["token"]
    yield payload["base_url"]
    for tunnel in [origin["tunnel"] for origin in payload["origins"]] + [payload["desk_url"]]:
        yield tunnel
        yield tunnel.split("//", 1)[1]
    for origin in payload["origins"]:
        yield origin["host"]


def _write_private(path: Path, text: str):
    path.write_text(text)
    path.chmod(OWNER_ONLY)


def _job_device_type(payload: dict) -> str:
    device_type = payload.get("device_type") or os.environ["DEFAULT_DEVICE_TYPE"]
    if not DEVICE_TYPE_SHAPE.fullmatch(device_type):
        raise SystemExit(f"the payload's device type is not a simulator device type id: {device_type!r}")
    return device_type


def main():
    run_dir = Path(sys.argv[1])
    (run_dir / "out" / "shots").mkdir(parents=True, exist_ok=True)
    payload = unseal(os.environ["PAYLOAD"], key=os.environ["IPHONE_RUN_KEY"])
    _mask(_secrets_of(payload))
    device_type = _job_device_type(payload)
    with open(os.environ["GITHUB_ENV"], "a") as job_env:
        job_env.write(f"DEVICE_TYPE={device_type}\n")
    _write_private(run_dir / "run.json", json.dumps(payload))
    hosts = sorted({origin["host"] for origin in payload["origins"]})
    (run_dir / "hosts_lines").write_text("".join(f"127.0.0.1 {host}\n" for host in hosts))
    print(f"unpacked: {len(payload['origins'])} origin(s) and the desk; phone {device_type.rsplit('.', 1)[1]}")


if __name__ == "__main__":
    main()
