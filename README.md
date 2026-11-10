# Personal finance via Plaid (UFCU + Robinhood)

Syncs your bank and brokerage data through [Plaid](https://plaid.com) into a local SQLite file
(`data/finance.db`) and exposes it to AI assistants as a **read-only** MCP server named `finance`.
Nothing here can move money. Your bank passwords are typed only into Plaid's own login window.

## 1. Get Plaid keys
1. Sign up at https://dashboard.plaid.com.
2. Copy your **client_id** and **secret** from *Developers → Keys*.
3. To use real accounts you need **Production** access: in the dashboard, request it and enable the
   **Transactions** and **Investments** products. (Sandbox works immediately but only has fake data.)
4. `copy .env.example .env` and fill in `PLAID_CLIENT_ID`, `PLAID_SECRET`, and `PLAID_ENV`
   (`sandbox` or `production`; the secret differs per environment).

## 2. Check your keys with fake data (optional)
```
.venv\Scripts\python sync.py --sandbox-demo
```

## 3. Connect UFCU and Robinhood
```
.venv\Scripts\python link_server.py
```
A page opens at http://localhost:8765. Click **Connect a bank** → pick UFCU; then
**Connect a brokerage** → pick Robinhood. Log in inside the Plaid window. Stop the server with Ctrl+C when done.

## 4. Ask questions
`.mcp.json` in this folder registers the server for MCP clients that open this folder.
Data auto-syncs when it's more than 6 hours old, or say "sync my accounts". Example questions:
- What's my net worth? How much cash do I have at UFCU?
- How much did I spend on food last month vs. the month before?
- What subscriptions am I paying for?
- How is my Robinhood portfolio doing? What's my gain on NVDA?

Tools: `sync_accounts`, `list_connections`, `list_accounts`, `net_worth`, `search_transactions`,
`spending_summary`, `recurring_charges`, `get_holdings`, `investment_activity`, `query_finance_db`.

## Notes
- First transaction sync can take a few minutes on Plaid's side; if it comes back empty, sync again later.
- If a bank asks you to log in again (`ITEM_LOGIN_REQUIRED`), rerun `link_server.py` and reconnect it.
- `.env` and `data/` hold secrets / your financial data and are git-ignored. Delete `data/finance.db` to wipe everything.
