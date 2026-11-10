"""Local page for connecting accounts with Plaid Link.

Run:  .venv\\Scripts\\python link_server.py   then open http://localhost:8765
You log in to your bank / Robinhood inside Plaid's own window; this app never sees your passwords.
Only the resulting Plaid access token is stored, in data/finance.db on this computer.
"""
from __future__ import annotations

import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from store import Plaid, PlaidError, connect

PORT = 8765
PAGE = (Path(__file__).parent / "link.html").read_text(encoding="utf-8")
ALLOWED_PRODUCTS = {"transactions", "investments"}

plaid = Plaid()


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = PAGE.replace("{{ENV}}", plaid.env).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/connections":
            conn = connect()
            rows = conn.execute("SELECT institution_name, products, created_at, last_synced, last_error "
                                "FROM items WHERE env=? ORDER BY created_at", (plaid.env,)).fetchall()
            conn.close()
            self._json(200, [dict(r) for r in rows])
        else:
            self.send_error(404)

    def do_POST(self):
        # Only accept requests from our own page.
        origin = self.headers.get("Origin", "")
        if origin not in (f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"):
            return self._json(403, {"error": "bad origin"})
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length) or b"{}")
        try:
            if self.path == "/api/link_token":
                products = [p for p in req.get("products", []) if p in ALLOWED_PRODUCTS] or ["transactions"]
                body = {
                    "client_name": "Personal Finance Assistant",
                    "user": {"client_user_id": "local-user"},
                    "products": products,
                    "country_codes": ["US"],
                    "language": "en",
                }
                if "transactions" in products:
                    body["transactions"] = {"days_requested": 730}
                self._json(200, {"link_token": plaid.post("/link/token/create", body)["link_token"]})
            elif self.path == "/api/exchange":
                ex = plaid.post("/item/public_token/exchange", {"public_token": req["public_token"]})
                products = [p for p in req.get("products", []) if p in ALLOWED_PRODUCTS]
                conn = connect()
                conn.execute("INSERT OR REPLACE INTO items (item_id, access_token, institution_name, products, env)"
                             " VALUES (?,?,?,?,?)",
                             (ex["item_id"], ex["access_token"], req.get("institution_name") or "Unknown",
                              ",".join(products), plaid.env))
                conn.commit()
                conn.close()
                self._json(200, {"ok": True})
            else:
                self.send_error(404)
        except PlaidError as e:
            self._json(400, {"error": str(e)})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Plaid Link page running at {url}  (env: {plaid.env})  - Ctrl+C to stop")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
