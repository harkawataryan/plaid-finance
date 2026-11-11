"""Expose the read-only finance MCP server through a private ngrok link, so web-based
MCP clients (browser and phone) can use it as a custom connector while this PC is on.

Run:  .venv\\Scripts\\pythonw.exe remote_server.py      (started at login by Task Scheduler)

Security model: the MCP endpoint lives at a long random path (the "secret link").
Every other path returns 404. Anyone with the full link can read your finances,
so keep it private; rotate it with:  .venv\\Scripts\\python remote_server.py --rotate
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
LOG = DATA / "remote.log"
SECRET_FILE = DATA / "connector_secret.txt"
URL_FILE = DATA / "connector_url.txt"
PORT = 8766

# pythonw has no console: send all output to a log file.
if sys.stdout is None or sys.stderr is None:
    _log = open(LOG, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log

import logging  # noqa: E402

logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


def get_secret(rotate: bool = False) -> str:
    if rotate or not SECRET_FILE.exists():
        SECRET_FILE.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return SECRET_FILE.read_text(encoding="utf-8").strip()


def find_ngrok() -> str:
    found = shutil.which("ngrok")
    if found:
        return found
    pkgs = Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "WinGet" / "Packages"
    for p in pkgs.glob("Ngrok.Ngrok*/ngrok.exe"):
        return str(p)
    raise SystemExit("ngrok not found")


def start_ngrok() -> tuple[subprocess.Popen, str]:
    proc = subprocess.Popen(
        [find_ngrok(), "http", str(PORT), "--host-header=rewrite", "--log=stdout"],
        stdout=open(DATA / "ngrok.log", "w", encoding="utf-8"), stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    for _ in range(60):
        time.sleep(1)
        try:
            with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as r:
                tunnels = json.load(r)["tunnels"]
            for t in tunnels:
                if t["public_url"].startswith("https://"):
                    return proc, t["public_url"]
        except OSError:
            pass
    proc.kill()
    raise SystemExit("ngrok did not come up; see data/ngrok.log")


def main() -> None:
    secret = get_secret(rotate="--rotate" in sys.argv)
    if "--rotate" in sys.argv:
        print("New secret saved. Restart the 'Plaid Finance Remote' task and update the connector URL in your MCP client.")
        return

    import server  # the same tools as the local server

    proc, public = start_ngrok()
    URL_FILE.write_text(f"{public}/{secret}/mcp\n", encoding="utf-8")
    logging.warning("serving at %s/<secret>/mcp", public)
    try:
        server.mcp.run(transport="streamable-http", host="127.0.0.1", port=PORT,
                       streamable_http_path=f"/{secret}/mcp", stateless_http=True)
    finally:
        proc.kill()


if __name__ == "__main__":
    main()
