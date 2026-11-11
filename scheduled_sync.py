"""Background sync entry point for Windows Task Scheduler (run with pythonw.exe, no console window).

Appends one line per run to data/sync.log.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

LOG = Path(__file__).resolve().parent / "data" / "sync.log"
LOG.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=LOG, level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    import sync  # imported after logging is set up, so errors land in the log

    stamp = dt.datetime.now().isoformat(timespec="seconds")
    try:
        results = sync.sync_all()
        summary = []
        for r in results:
            parts = [r["institution"]]
            if "transactions" in r:
                t = r["transactions"]
                parts.append(f"+{t['added']} ~{t['modified']} -{t['removed']} txns")
            if "investments" in r:
                parts.append(f"{r['investments']['holdings']} holdings")
            if r.get("error"):
                parts.append(f"ERROR {r['error']}")
            parts += r.get("notes", [])
            summary.append(" ".join(parts))
        line = f"{stamp} ok  | " + " | ".join(summary)
    except Exception as e:  # noqa: BLE001
        line = f"{stamp} FAILED | {e!r}"
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    # Keep the log from growing forever: last 500 lines.
    lines = LOG.read_text(encoding="utf-8").splitlines()
    if len(lines) > 500:
        LOG.write_text("\n".join(lines[-500:]) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
