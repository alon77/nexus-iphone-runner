"""device_types — one line naming every iPhone the runtime can boot, with its screen in points, shortest first, so
`iphone up` shows which `--device <name>` values exist (`guide iphone:session`). Run as
`python runner/device_types.py <runtime id>` on the runner; reads `xcrun simctl list -j` and each device type's own
profile.plist (the screen size lives only there).
"""

import json
import plistlib
import subprocess
import sys
from pathlib import Path

SCREEN_FIELDS = ("mainScreenWidth", "mainScreenHeight", "mainScreenScale")
PROFILE = Path("Contents") / "Resources" / "profile.plist"


def _simctl(*args) -> dict:
    listed = subprocess.run(["xcrun", "simctl", "list", "-j", *args], capture_output=True, text=True, check=True)
    return json.loads(listed.stdout)


def _points(device_type: dict) -> tuple:
    profile_path = Path(device_type.get("bundlePath", "")) / PROFILE
    profile = plistlib.loads(profile_path.read_bytes()) if profile_path.is_file() else {}
    width, height, scale = (profile.get(field) or 0 for field in SCREEN_FIELDS)
    return (round(width / scale), round(height / scale)) if scale else (0, 0)


def iphones(runtime_id: str) -> list:
    runtime = next(runtime for runtime in _simctl("runtimes")["runtimes"] if runtime["identifier"] == runtime_id)
    supported = {device["identifier"] for device in runtime.get("supportedDeviceTypes", [])}
    known = {device["identifier"]: device for device in _simctl("devicetypes")["devicetypes"]}
    rows = [(known[identifier]["name"], *_points(known[identifier])) for identifier in supported
            if known.get(identifier, {}).get("productFamily") == "iPhone"]
    return sorted(rows, key=lambda row: (row[2], row[1]))


def main():
    print("; ".join(f"{name} {width}x{height}" for name, width, height in iphones(sys.argv[1])))


if __name__ == "__main__":
    main()
