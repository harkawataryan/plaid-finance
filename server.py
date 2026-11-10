"""MCP server that lets an AI assistant read your synced finances (UFCU, Robinhood, ... via Plaid).

Read-only: there are no tools that move money or change anything at your bank.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import sys

from mcp.server.mcpserver import MCPServer

import sync
from store import connect

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
mcp = MCPServer("finance", instructions="Read-only access to the user's synced bank (UFCU) and brokerage (Robinhood) data via Plaid. Transaction amount > 0 means money spent.")

AUTO_SYNC_HOURS = 6
LIABILITY_TYPES = ("credit", "loan")
# Plaid categories that are money moving between your own accounts, not real spending/income.
TRANSFER_CATS = ("TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS")


def _rows(sql: str, params=()) -> list[dict]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _auto_sync() -> str | None:
    """Sync if data is stale. Returns a warning string on failure, never raises."""
    try:
        age = sync.last_sync_age_hours()
        if age is None or age > AUTO_SYNC_HOURS:
            sync.sync_all()
    except Exception as e:  # noqa: BLE001
        return f"auto-sync failed, data may be stale: {e}"
    return None


def _default_range(start_date: str | None, end_date: str | None, days: int = 30) -> tuple[str, str]:
    end = end_date or dt.date.today().isoformat()
    start = start_date or (dt.date.fromisoformat(end) - dt.timedelta(days=days)).isoformat()
    return start, end


@mcp.tool()
def sync_accounts() -> list[dict]:
    """Pull the latest balances, transactions and investments from all linked institutions now."""
    return sync.sync_all()


@mcp.tool()
def list_connections() -> list[dict]:
    """Linked institutions, which data each provides, when each last synced, and any error."""
    return _rows("SELECT institution_name, products, env, created_at, last_synced, last_error FROM items")


@mcp.tool()
def list_accounts() -> dict:
    """All accounts with current/available balances, type (depository, credit, loan, investment) and institution."""
    warn = _auto_sync()
    accts = _rows("""SELECT institution_name, name, mask, type, subtype, current_balance, available_balance,
                            credit_limit, currency, updated_at FROM accounts ORDER BY institution_name, type, name""")
    return {"accounts": accts, "warning": warn}


@mcp.tool()
def net_worth() -> dict:
    """Net worth = cash + investments - credit card and loan balances, with a breakdown and recent history."""
    warn = _auto_sync()
    by_type = _rows("SELECT type, SUM(current_balance) AS total FROM accounts GROUP BY type")
    assets = sum(r["total"] or 0 for r in by_type if r["type"] not in LIABILITY_TYPES)
    liabilities = sum(r["total"] or 0 for r in by_type if r["type"] in LIABILITY_TYPES)
    history = _rows(f"""SELECT h.date,
                          ROUND(SUM(CASE WHEN a.type IN {LIABILITY_TYPES} THEN -h.current_balance
                                         ELSE h.current_balance END), 2) AS net_worth
                        FROM balance_history h JOIN accounts a USING(account_id)
                        GROUP BY h.date ORDER BY h.date DESC LIMIT 90""")
    return {"net_worth": round(assets - liabilities, 2), "assets": round(assets, 2),
            "liabilities": round(liabilities, 2), "by_account_type": by_type,
            "history_newest_first": history, "warning": warn}


@mcp.tool()
def search_transactions(start_date: str | None = None, end_date: str | None = None, text: str | None = None,
                        category: str | None = None, account: str | None = None,
                        min_amount: float | None = None, max_amount: float | None = None,
                        limit: int = 200) -> dict:
    """Bank/card transactions, newest first. Dates are YYYY-MM-DD (default: last 30 days).

    amount > 0 = money spent / left the account; amount < 0 = money received (paycheck, refund).
    text matches merchant or description. category is a Plaid primary category such as
    FOOD_AND_DRINK, GENERAL_MERCHANDISE, TRANSPORTATION, RENT_AND_UTILITIES, INCOME, TRANSFER_OUT,
    ENTERTAINMENT, TRAVEL, LOAN_PAYMENTS, PERSONAL_CARE, MEDICAL, GENERAL_SERVICES.
    account matches account name, mask or institution.
    """
    warn = _auto_sync()
    start, end = _default_range(start_date, end_date)
    sql = """SELECT t.date, COALESCE(t.merchant_name, t.name) AS merchant, t.name AS description, t.amount,
                    t.category, t.category_detailed, t.pending, a.institution_name, a.name AS account, a.mask
             FROM transactions t LEFT JOIN accounts a USING(account_id)
             WHERE t.date BETWEEN ? AND ?"""
    p: list = [start, end]
    if text:
        sql += " AND (t.merchant_name LIKE ? OR t.name LIKE ?)"
        p += [f"%{text}%"] * 2
    if category:
        sql += " AND (t.category = ? OR t.category_detailed LIKE ?)"
        p += [category.upper(), f"%{category.upper()}%"]
    if account:
        sql += " AND (a.name LIKE ? OR a.mask = ? OR a.institution_name LIKE ?)"
        p += [f"%{account}%", account, f"%{account}%"]
    if min_amount is not None:
        sql += " AND t.amount >= ?"
        p.append(min_amount)
    if max_amount is not None:
        sql += " AND t.amount <= ?"
        p.append(max_amount)
    sql += " ORDER BY t.date DESC LIMIT ?"
    p.append(min(limit, 2000))
    rows = _rows(sql, p)
    return {"start_date": start, "end_date": end, "count": len(rows), "transactions": rows, "warning": warn}


@mcp.tool()
def spending_summary(start_date: str | None = None, end_date: str | None = None,
                     group_by: str = "category", include_transfers: bool = False) -> dict:
    """Totals of spending and income over a date range (default last 30 days).

    group_by: category | category_detailed | merchant | month | week | account.
    Transfers between your own accounts and credit-card/loan payments are excluded unless include_transfers.
    spent = money out, received = money in, net = spent - received.
    """
    warn = _auto_sync()
    start, end = _default_range(start_date, end_date)
    key = {
        "category": "t.category",
        "category_detailed": "t.category_detailed",
        "merchant": "COALESCE(t.merchant_name, t.name)",
        "month": "substr(t.date, 1, 7)",
        "week": "strftime('%Y-W%W', t.date)",
        "account": "a.institution_name || ' ' || a.name",
    }.get(group_by)
    if not key:
        raise ValueError("group_by must be category, category_detailed, merchant, month, week or account")
    where = "t.date BETWEEN ? AND ?"
    if not include_transfers:
        where += f" AND COALESCE(t.category, '') NOT IN {TRANSFER_CATS}"
    rows = _rows(f"""SELECT {key} AS grp,
                        ROUND(SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END), 2) AS spent,
                        ROUND(SUM(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END), 2) AS received,
                        COUNT(*) AS n
                     FROM transactions t LEFT JOIN accounts a USING(account_id)
                     WHERE {where} GROUP BY grp ORDER BY spent DESC""", (start, end))
    return {"start_date": start, "end_date": end, "group_by": group_by,
            "total_spent": round(sum(r["spent"] for r in rows), 2),
            "total_received": round(sum(r["received"] for r in rows), 2),
            "groups": rows, "warning": warn}


@mcp.tool()
def recurring_charges(months: int = 4) -> list[dict]:
    """Likely subscriptions / recurring bills: merchants charged in at least 3 different months recently."""
    _auto_sync()
    since = (dt.date.today() - dt.timedelta(days=31 * months)).isoformat()
    return _rows(f"""SELECT COALESCE(merchant_name, name) AS merchant, COUNT(*) AS charges,
                        COUNT(DISTINCT substr(date,1,7)) AS months_seen,
                        ROUND(AVG(amount), 2) AS avg_amount, MAX(date) AS last_charged, category
                     FROM transactions
                     WHERE date >= ? AND amount > 0 AND COALESCE(category,'') NOT IN {TRANSFER_CATS}
                     GROUP BY merchant HAVING months_seen >= 3
                     ORDER BY avg_amount DESC""", (since,))


@mcp.tool()
def get_holdings() -> dict:
    """Current investment positions (e.g. Robinhood): ticker, quantity, price, value, cost basis, gain/loss."""
    warn = _auto_sync()
    rows = _rows("""SELECT a.institution_name, a.name AS account, s.ticker, s.name, s.type, h.quantity,
                           h.price, h.value, h.cost_basis,
                           ROUND(h.value - h.cost_basis, 2) AS unrealized_gain,
                           CASE WHEN h.cost_basis > 0 THEN ROUND(100.0*(h.value - h.cost_basis)/h.cost_basis, 2) END
                             AS unrealized_gain_pct,
                           h.price_as_of
                    FROM holdings h JOIN securities s USING(security_id) LEFT JOIN accounts a USING(account_id)
                    ORDER BY h.value DESC""")
    total = sum(r["value"] or 0 for r in rows)
    for r in rows:
        r["pct_of_portfolio"] = round(100 * (r["value"] or 0) / total, 2) if total else None
    return {"total_value": round(total, 2), "positions": rows, "warning": warn}


@mcp.tool()
def investment_activity(start_date: str | None = None, end_date: str | None = None,
                        ticker: str | None = None, limit: int = 300) -> dict:
    """Buys, sells, dividends, deposits and fees in brokerage accounts (default last 365 days).

    amount > 0 = cash left the brokerage account (e.g. a buy); amount < 0 = cash came in (sell, dividend).
    """
    warn = _auto_sync()
    start, end = _default_range(start_date, end_date, days=365)
    sql = """SELECT t.date, s.ticker, s.name AS security, t.type, t.subtype, t.quantity, t.price, t.amount,
                    t.fees, t.name AS description, a.institution_name
             FROM investment_transactions t LEFT JOIN securities s USING(security_id)
             LEFT JOIN accounts a USING(account_id) WHERE t.date BETWEEN ? AND ?"""
    p: list = [start, end]
    if ticker:
        sql += " AND s.ticker = ?"
        p.append(ticker.upper())
    sql += " ORDER BY t.date DESC LIMIT ?"
    p.append(min(limit, 2000))
    rows = _rows(sql, p)
    return {"start_date": start, "end_date": end, "count": len(rows), "transactions": rows, "warning": warn}


@mcp.tool()
def query_finance_db(sql: str) -> list[dict]:
    """Run a read-only SQL SELECT on the local finance database for questions the other tools don't cover.

    Tables: accounts(account_id, institution_name, name, mask, type, subtype, current_balance,
      available_balance, credit_limit, currency, updated_at)
    balance_history(date, account_id, current_balance)
    transactions(transaction_id, account_id, date, authorized_date, name, merchant_name, amount,
      currency, category, category_detailed, payment_channel, pending)  -- amount>0 = money out
    securities(security_id, name, ticker, type, close_price, close_price_as_of)
    holdings(account_id, security_id, quantity, price, value, cost_basis, price_as_of)
    investment_transactions(investment_transaction_id, account_id, security_id, date, name, type,
      subtype, quantity, price, amount, fees)
    Dates are 'YYYY-MM-DD' text. Results are capped at 1000 rows.
    """
    if not re.match(r"^\s*(select|with)\b", sql, re.I) or ";" in sql.strip().rstrip(";"):
        raise ValueError("Only a single SELECT / WITH query is allowed")
    if re.search(r"\b(access_token|items)\b", sql, re.I):
        raise ValueError("The items table (connection secrets) is not queryable")
    conn = connect(readonly=True)
    try:
        cur = conn.execute(sql)
        return [dict(r) for r in cur.fetchmany(1000)]
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()
