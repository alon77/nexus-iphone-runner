"""unpack — the runner's first step: open the sealed dispatch payload, mask every value in the job log, lay out the
run dir (run.json, spec.py, hosts_lines, out/). Reads PAYLOAD and IPHONE_RUN_KEY from the env, never from argv, so
neither reaches the job log. `guide iphone:tunnel`.
"""

import json
import os
import re
import sys
from pathlib import Path

from sealed import unseal

OWNER_ONLY = 0o600
SHORTEST_MASKED_LITERAL = 6
QUOTED_LITERAL = re.compile(r"""(['"])(.+?)\1""")


def _mask(values):
    for value in values:
        for line in str(value).splitlines():
            if line.strip():
                print(f"::add-mask::{line}")


def _secrets_of(payload: dict):
    yield payload["token"]
    yield payload["base_url"]
    for origin in payload["origins"]:
        yield origin["host"]
        yield origin["tunnel"]
        yield origin["tunnel"].split("//", 1)[1]
    for _, literal in QUOTED_LITERAL.findall(payload["spec_source"]):
        if len(literal) >= SHORTEST_MASKED_LITERAL:
            yield literal


def _write_private(path: Path, text: str):
    path.write_text(text)
    path.chmod(OWNER_ONLY)


def main():
    run_dir = Path(sys.argv[1])
    (run_dir / "out" / "shots").mkdir(parents=True, exist_ok=True)
    payload = unseal(os.environ["PAYLOAD"], key=os.environ["IPHONE_RUN_KEY"])
    _mask(_secrets_of(payload))
    _write_private(run_dir / "spec.py", payload.pop("spec_source"))
    _write_private(run_dir / "run.json", json.dumps(payload))
    hosts = sorted({origin["host"] for origin in payload["origins"]})
    (run_dir / "hosts_lines").write_text("".join(f"127.0.0.1 {host}\n" for host in hosts))
    print(f"unpacked: {len(payload['origins'])} origin(s), spec {payload['spec_name'].split(':')[-1]}")


if __name__ == "__main__":
    main()
