"""Pull balances, transactions and investments from Plaid into the local SQLite store.

Run directly:  .venv\\Scripts\\python sync.py
"""
from __future__ import annotations

import datetime as dt
import logging
import sys

from store import Plaid, PlaidError, connect

log = logging.getLogger("plaid-finance.sync")

INVESTMENT_HISTORY_DAYS = 730
# Errors that mean "data not ready yet / product not on this item" rather than a broken connection.
SOFT_ERRORS = {"PRODUCT_NOT_READY", "PRODUCTS_NOT_SUPPORTED", "NO_INVESTMENT_ACCOUNTS",
               "NO_INVESTMENT_AUTH_ACCOUNTS", "ADDITIONAL_CONSENT_REQUIRED", "NO_ACCOUNTS"}


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def sync_accounts(plaid: Plaid, conn, item) -> int:
    data = plaid.post("/accounts/get", {"access_token": item["access_token"]})
    today = dt.date.today().isoformat()
    for a in data["accounts"]:
        b = a.get("balances") or {}
        conn.execute(
            """INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_id) DO UPDATE SET
                 name=excluded.name, official_name=excluded.official_name, mask=excluded.mask,
                 type=excluded.type, subtype=excluded.subtype, current_balance=excluded.current_balance,
                 available_balance=excluded.available_balance, credit_limit=excluded.credit_limit,
                 currency=excluded.currency, updated_at=excluded.updated_at,
                 institution_name=excluded.institution_name""",
            (a["account_id"], item["item_id"], item["institution_name"], a.get("name"),
             a.get("official_name"), a.get("mask"), a.get("type"), a.get("subtype"),
             b.get("current"), b.get("available"), b.get("limit"),
             b.get("iso_currency_code") or b.get("unofficial_currency_code"), _now()),
        )
        conn.execute("INSERT OR REPLACE INTO balance_history VALUES (?,?,?)",
                     (today, a["account_id"], b.get("current")))
    return len(data["accounts"])


def sync_transactions(plaid: Plaid, conn, item) -> dict:
    start_cursor = item["txn_cursor"]
    for _attempt in range(3):
        cursor, added, modified, removed = start_cursor, [], [], []
        try:
            while True:
                body = {"access_token": item["access_token"], "count": 500}
                if cursor:
                    body["cursor"] = cursor
                page = plaid.post("/transactions/sync", body)
                added += page["added"]
                modified += page["modified"]
                removed += page["removed"]
                cursor = page["next_cursor"]
                if not page["has_more"]:
                    break
            break
        except PlaidError as e:
            if e.code != "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
                raise
    else:
        raise RuntimeError("transactions kept changing during sync; try again shortly")

    for t in added + modified:
        pfc = t.get("personal_finance_category") or {}
        conn.execute(
            "INSERT OR REPLACE INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (t["transaction_id"], t["account_id"], t["date"], t.get("authorized_date"),
             t.get("name"), t.get("merchant_name"), t["amount"],
             t.get("iso_currency_code") or t.get("unofficial_currency_code"),
             pfc.get("primary"), pfc.get("detailed"), t.get("payment_channel"),
             int(bool(t.get("pending")))),
        )
    for r in removed:
        conn.execute("DELETE FROM transactions WHERE transaction_id=?", (r["transaction_id"],))
    conn.execute("UPDATE items SET txn_cursor=? WHERE item_id=?", (cursor, item["item_id"]))
    return {"added": len(added), "modified": len(modified), "removed": len(removed)}


def _upsert_securities(conn, securities) -> None:
    for s in securities:
        conn.execute(
            "INSERT OR REPLACE INTO securities VALUES (?,?,?,?,?,?)",
            (s["security_id"], s.get("name"), s.get("ticker_symbol"), s.get("type"),
             s.get("close_price"), s.get("close_price_as_of")),
        )


def sync_investments(plaid: Plaid, conn, item) -> dict:
    tok = item["access_token"]
    h = plaid.post("/investments/holdings/get", {"access_token": tok})
    _upsert_securities(conn, h["securities"])
    acct_ids = [a["account_id"] for a in h["accounts"]]
    conn.executemany("DELETE FROM holdings WHERE account_id=?", [(i,) for i in acct_ids])
    for x in h["holdings"]:
        conn.execute(
            "INSERT OR REPLACE INTO holdings VALUES (?,?,?,?,?,?,?)",
            (x["account_id"], x["security_id"], x.get("quantity"), x.get("institution_price"),
             x.get("institution_value"), x.get("cost_basis"), x.get("institution_price_as_of")),
        )

    end = dt.date.today()
    start = end - dt.timedelta(days=INVESTMENT_HISTORY_DAYS)
    offset, total, n = 0, None, 0
    while total is None or offset < total:
        page = plaid.post("/investments/transactions/get", {
            "access_token": tok, "start_date": start.isoformat(), "end_date": end.isoformat(),
            "options": {"count": 500, "offset": offset},
        })
        _upsert_securities(conn, page["securities"])
        total = page["total_investment_transactions"]
        for t in page["investment_transactions"]:
            conn.execute(
                "INSERT OR REPLACE INTO investment_transactions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (t["investment_transaction_id"], t["account_id"], t.get("security_id"), t["date"],
                 t.get("name"), t.get("type"), t.get("subtype"), t.get("quantity"),
                 t.get("price"), t.get("amount"), t.get("fees")),
            )
        got = len(page["investment_transactions"])
        offset += got
        n += got
        if got == 0:
            break
    return {"holdings": len(h["holdings"]), "investment_transactions": n}


def sync_all() -> list[dict]:
    """Sync every linked institution. Returns one summary dict per institution."""
    plaid = Plaid()
    conn = connect()
    results = []
    items = conn.execute("SELECT * FROM items WHERE env=?", (plaid.env,)).fetchall()
    for item in items:
        res = {"institution": item["institution_name"], "notes": []}
        products = set((item["products"] or "").split(","))
        steps = [("accounts", sync_accounts)]
        if "transactions" in products:
            steps.append(("transactions", sync_transactions))
        if "investments" in products:
            steps.append(("investments", sync_investments))
        hard_error = None
        for name, fn in steps:
            try:
                res[name] = fn(plaid, conn, item)
                conn.commit()
            except PlaidError as e:
                conn.rollback()
                if e.code in SOFT_ERRORS:
                    res["notes"].append(f"{name}: {e}")
                else:
                    hard_error = f"{name}: {e}"
                    res["error"] = hard_error
                    if e.code == "ITEM_LOGIN_REQUIRED":
                        res["error"] += " -> re-link this institution with link_server.py"
                    break
        conn.execute("UPDATE items SET last_synced=?, last_error=? WHERE item_id=?",
                     (_now(), hard_error, item["item_id"]))
        conn.commit()
        results.append(res)
    conn.close()
    return results


def last_sync_age_hours() -> float | None:
    conn = connect()
    row = conn.execute("SELECT MIN(last_synced) FROM items").fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    return (dt.datetime.now() - dt.datetime.fromisoformat(row[0])).total_seconds() / 3600


def sandbox_demo() -> None:
    """Create fake sandbox connections (a bank + a brokerage) to check that your keys work."""
    plaid = Plaid()
    if plaid.env != "sandbox":
        sys.exit("--sandbox-demo only works with PLAID_ENV=sandbox")
    conn = connect()
    for inst, products, label in [("ins_109508", ["transactions"], "Sandbox Bank (demo)"),
                                  ("ins_115616", ["investments"], "Sandbox Brokerage (demo)")]:
        pt = plaid.post("/sandbox/public_token/create",
                        {"institution_id": inst, "initial_products": products})["public_token"]
        ex = plaid.post("/item/public_token/exchange", {"public_token": pt})
        conn.execute("INSERT OR REPLACE INTO items (item_id, access_token, institution_name, products, env)"
                     " VALUES (?,?,?,?,?)",
                     (ex["item_id"], ex["access_token"], label, ",".join(products), "sandbox"))
        conn.commit()
        print(f"linked {label}")
    conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if "--sandbox-demo" in sys.argv:
        sandbox_demo()
    import json
    print(json.dumps(sync_all(), indent=2))
