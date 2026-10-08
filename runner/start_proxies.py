"""start_proxies — runs under sudo on the GitHub Mac: one forward_proxy per origin on its real port (443, 6001),
terminating Safari's TLS with the run's leaf cert, adding the run token and forwarding to that origin's cloudflared
tunnel. Then proves each hop end to end: a request through the proxy, the tunnel and Alon's gate must reach the dev
origin (any answer but 403 or a 5xx). `guide iphone:tunnel`.
"""

import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LISTEN_HOST = "127.0.0.1"
PORT_WAIT_S = 30
PROBE_TIMEOUT_S = 30
POLL_S = 0.5
FORBIDDEN = 403
FIRST_SERVER_ERROR = 500
PROXY_SCRIPT = Path(__file__).with_name("forward_proxy.py")


def _proxy_command(run_dir: Path, origin: dict) -> list:
    tunnel_host = origin["tunnel"].split("//", 1)[1]
    return [sys.executable, str(PROXY_SCRIPT), "--listen-host", LISTEN_HOST, "--listen-port", str(origin["port"]),
            "--upstream", origin["tunnel"], "--upstream-host", tunnel_host, "--added-token-env", "RUN_TOKEN",
            "--cert", str(run_dir / "leaf.pem"), "--key", str(run_dir / "leaf.key"), "--verify-tls", "--cache-static"]


def _start(run_dir: Path, origin: dict):
    log = open(run_dir / "out" / f"proxy_{origin['port']}.log", "w")
    subprocess.Popen(_proxy_command(run_dir, origin), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def _wait_listening(port: int):
    deadline = time.monotonic() + PORT_WAIT_S
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((LISTEN_HOST, port), timeout=POLL_S):
                return
        except OSError:
            time.sleep(POLL_S)
    raise SystemExit(f"runner proxy on port {port} never listened — see out/proxy_{port}.log")


def _probe_status(run_dir: Path, origin: dict) -> int:
    context = ssl.create_default_context(cafile=str(run_dir / "ca.pem"))
    url = f"https://{origin['host']}:{origin['port']}/"
    try:
        with urllib.request.urlopen(url, context=context, timeout=PROBE_TIMEOUT_S) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def main():
    run_dir = Path(sys.argv[1])
    run = json.loads((run_dir / "run.json").read_text())
    os.environ["RUN_TOKEN"] = run["token"]
    for origin in run["origins"]:
        _start(run_dir, origin)
    for origin in run["origins"]:
        _wait_listening(origin["port"])
        status = _probe_status(run_dir, origin)
        if status == FORBIDDEN or status >= FIRST_SERVER_ERROR:
            raise SystemExit(f"port {origin['port']}: the path to Alon's dev origin answered {status}")
        print(f"port {origin['port']}: dev origin reached through the tunnel ({status})")


if __name__ == "__main__":
    main()
