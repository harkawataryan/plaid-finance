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
