"""Local SQLite store and Plaid HTTP client shared by the link page, sync and MCP server."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DB_PATH = Path(os.getenv("FINANCE_DB", ROOT / "data" / "finance.db"))

PLAID_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id          TEXT PRIMARY KEY,
    access_token     TEXT NOT NULL,
    institution_name TEXT,
    products         TEXT,              -- comma list: transactions,investments
    env              TEXT,
    created_at       TEXT DEFAULT (datetime('now')),
    txn_cursor       TEXT,
    last_synced      TEXT,
    last_error       TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
    account_id        TEXT PRIMARY KEY,
    item_id           TEXT,
    institution_name  TEXT,
    name              TEXT,
    official_name     TEXT,
    mask              TEXT,
    type              TEXT,             -- depository | credit | loan | investment | other
    subtype           TEXT,
    current_balance   REAL,
    available_balance REAL,
    credit_limit      REAL,
    currency          TEXT,
    updated_at        TEXT
);
CREATE TABLE IF NOT EXISTS balance_history (
    date            TEXT,
    account_id      TEXT,
    current_balance REAL,
    PRIMARY KEY (date, account_id)
);
-- amount > 0 means money LEFT the account (spending); amount < 0 means money came IN.
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id    TEXT PRIMARY KEY,
    account_id        TEXT,
    date              TEXT,
    authorized_date   TEXT,
    name              TEXT,
    merchant_name     TEXT,
    amount            REAL,
    currency          TEXT,
    category          TEXT,             -- Plaid personal_finance_category.primary, e.g. FOOD_AND_DRINK
    category_detailed TEXT,
    payment_channel   TEXT,
    pending           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(date);
CREATE TABLE IF NOT EXISTS securities (
    security_id       TEXT PRIMARY KEY,
    name              TEXT,
    ticker            TEXT,
    type              TEXT,             -- equity | etf | mutual fund | cryptocurrency | cash | ...
    close_price       REAL,
    close_price_as_of TEXT
);
CREATE TABLE IF NOT EXISTS holdings (
    account_id  TEXT,
    security_id TEXT,
    quantity    REAL,
    price       REAL,
    value       REAL,
    cost_basis  REAL,
    price_as_of TEXT,
    PRIMARY KEY (account_id, security_id)
);
CREATE TABLE IF NOT EXISTS investment_transactions (
    investment_transaction_id TEXT PRIMARY KEY,
    account_id  TEXT,
    security_id TEXT,
    date        TEXT,
    name        TEXT,
    type        TEXT,                   -- buy | sell | cash | fee | transfer | cancel
    subtype     TEXT,
    quantity    REAL,
    price       REAL,
    amount      REAL,                   -- > 0 cash out of account (e.g. buy), < 0 cash in (e.g. sell, dividend)
    fees        REAL
);
"""


def connect(readonly: bool = False) -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


class PlaidError(Exception):
    def __init__(self, data: dict):
        self.code = data.get("error_code", "UNKNOWN")
        self.data = data
        super().__init__(f"{self.code}: {data.get('error_message', data)}")


class Plaid:
    def __init__(self) -> None:
        self.env = os.getenv("PLAID_ENV", "sandbox").lower()
        self.client_id = os.getenv("PLAID_CLIENT_ID", "")
        self.secret = os.getenv("PLAID_SECRET", "")
        if self.env not in PLAID_HOSTS:
            raise RuntimeError(f"PLAID_ENV must be one of {list(PLAID_HOSTS)}")
        if not self.client_id or not self.secret:
            raise RuntimeError(f"Set PLAID_CLIENT_ID and PLAID_SECRET in {ROOT / '.env'}")
        self.http = httpx.Client(base_url=PLAID_HOSTS[self.env], timeout=60)

    def post(self, path: str, body: dict | None = None) -> dict:
        payload = {"client_id": self.client_id, "secret": self.secret, **(body or {})}
        r = self.http.post(path, json=payload)
        data = r.json()
        if r.status_code != 200:
            raise PlaidError(data)
        return data
