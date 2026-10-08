"""seal_output — the runner's last step: tar out/ (results.json, shots, Appium log, Safari console, proxy logs) and
seal it, so the uploaded artifact is ciphertext a stranger cannot read. `iphone run` downloads and opens it."""

import io
import os
import sys
import tarfile
from pathlib import Path

from sealed import seal_bytes


def main():
    run_dir = Path(sys.argv[1])
    bundle = io.BytesIO()
    with tarfile.open(fileobj=bundle, mode="w:gz") as tar:
        tar.add(run_dir / "out", arcname="out")
    (run_dir / "out.sealed").write_bytes(seal_bytes(bundle.getvalue(), key=os.environ["IPHONE_RUN_KEY"]))
    print(f"sealed {len(bundle.getvalue())} bytes of run output")


if __name__ == "__main__":
    main()
