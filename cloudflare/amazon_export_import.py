"""Import product names from Amazon's own data export into the Worker's database.

Get the export: amazon.com -> Account -> "Request Your Data" (Privacy Central) -> "Your Orders".
Amazon emails a zip within a few days. Unzip it and run from this folder:

    ..\\.venv\\Scripts\\python amazon_export_import.py "path\\to\\Retail.OrderHistory.1.csv"

Each CSV row is one product in one order. This writes amazon_items rows (and an amazon_orders row
for orders older than the Gmail sync) and runs them against D1 with wrangler. Safe to re-run.
"""
from __future__ import annotations

import csv
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def pick(row: dict[str, str], *names: str) -> str:
    for n in names:
        for k, v in row.items():
            if k and k.strip().lower() == n.lower() and v not in (None, "", "Not Available"):
                return v.strip()
    return ""


def num(s: str) -> float | None:
    s = s.replace("$", "").replace(",", "").strip().strip("'")
    try:
        return float(s)
    except ValueError:
        return None


def q(v) -> str:
    if v is None or v == "":
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    rows = list(csv.DictReader(open(sys.argv[1], encoding="utf-8-sig", newline="")))
    sql: list[str] = []
    orders: dict[str, str] = {}
    for r in rows:
        oid = pick(r, "Order ID")
        title = pick(r, "Product Name", "Title")
        if not oid or not title:
            continue
        orders.setdefault(oid, pick(r, "Order Date")[:10])
        sql.append("INSERT OR REPLACE INTO amazon_items VALUES ({});".format(", ".join(map(q, [
            oid, title, num(pick(r, "Quantity")) or 1, num(pick(r, "Unit Price", "Purchase Price Per Unit")),
            pick(r, "ASIN", "ASIN/ISBN"), "export"]))))
    for oid, date in orders.items():
        sql.append(f"INSERT OR IGNORE INTO amazon_orders (order_id, order_date, status) VALUES ({q(oid)}, {q(date)}, 'delivered');")
    if not sql:
        sys.exit("No rows with an Order ID and Product Name were found; is this the Retail.OrderHistory CSV?")

    fd, path = tempfile.mkstemp(suffix=".sql")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(sql))
        print(f"Importing {len(rows)} rows across {len(orders)} orders...")
        r = subprocess.run(f'npx wrangler d1 execute finance --remote --file "{path}"', cwd=HERE, shell=True)
    finally:
        os.remove(path)
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
