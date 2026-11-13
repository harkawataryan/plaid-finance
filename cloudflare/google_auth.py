"""Connect Gmail (read-only) to the Worker so it can read Amazon order emails.

One-time setup in Google Cloud (https://console.cloud.google.com):
  1. Create a project, then enable the "Gmail API" (APIs & Services -> Library).
  2. OAuth consent screen: External, add your Gmail as a test user, then press "Publish app".
     (Unpublished "Testing" apps get refresh tokens that expire after 7 days. Publishing an app only
     you use needs no review; Google just shows an "unverified app" warning you click through.)
  3. Credentials -> Create credentials -> OAuth client ID -> Application type "Desktop app".

Then run from this folder:  ..\\.venv\\Scripts\\python google_auth.py
It opens Google's sign-in page, asks only for gmail.readonly, and stores GOOGLE_CLIENT_ID,
GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN as Worker secrets (never printed, never written to the repo).
Run it again any time the Worker reports "invalid_grant".
"""
from __future__ import annotations

import getpass
import http.server
import json
import os
import secrets
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def get_code(client_id: str) -> tuple[str, str]:
    state = secrets.token_urlsafe(16)
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if "code" in q or "error" in q:
                result.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h3>Done - you can close this tab and go back to the terminal.</h3>")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
    })
    print("Opening Google sign-in. If no browser opens, visit:\n" + url)
    webbrowser.open(url)
    while not result:
        server.handle_request()
    server.server_close()
    if result.get("state") != state:
        sys.exit("State mismatch; aborting.")
    if "error" in result:
        sys.exit(f"Google returned an error: {result['error']}")
    return result["code"], redirect


def main() -> None:
    client_id = input("OAuth client ID (Desktop app): ").strip()
    client_secret = getpass.getpass("OAuth client secret (hidden): ").strip()
    if not client_id or not client_secret:
        sys.exit("Client ID and secret are required.")
    code, redirect = get_code(client_id)
    body = urllib.parse.urlencode({
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code",
    }).encode()
    with urllib.request.urlopen("https://oauth2.googleapis.com/token", body) as r:
        tokens = json.load(r)
    refresh = tokens.get("refresh_token")
    if not refresh:
        sys.exit("Google did not return a refresh token. Remove the app at https://myaccount.google.com/permissions and retry.")

    data = {"GOOGLE_CLIENT_ID": client_id, "GOOGLE_CLIENT_SECRET": client_secret, "GOOGLE_REFRESH_TOKEN": refresh}
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        print("Saving secrets to the Worker...")
        r = subprocess.run(f'npx wrangler secret bulk "{path}"', cwd=HERE, shell=True)
    finally:
        os.remove(path)
    print("\nGmail connected. Ask Claude to run sync_amazon." if r.returncode == 0 else f"\nwrangler failed (exit {r.returncode}).")


if __name__ == "__main__":
    main()
