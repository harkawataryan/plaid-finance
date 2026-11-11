"""Deploy the Worker together with its secrets, without the secrets ever being printed.

Run from this folder:  ..\\.venv\\Scripts\\python deploy_with_secrets.py
- PLAID_CLIENT_ID / PLAID_SECRET are read from ../.env
- GITHUB_CLIENT_ID is the OAuth app's public ID
- GITHUB_CLIENT_SECRET is pasted by you (hidden)
- COOKIE_ENCRYPTION_KEY is generated
Secrets go to a temporary file that is deleted right after the deploy.
Later deploys can just use `npx wrangler deploy` (secrets persist).
"""
from __future__ import annotations

import getpass
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GITHUB_CLIENT_ID = "Ov23liIfRSa3Yt6x71bb"


def read_env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main() -> None:
    env = read_env(HERE.parent / ".env")
    if env.get("PLAID_ENV", "production") != "production":
        sys.exit("../.env is not set to production; the Worker expects production Plaid keys.")
    gh_secret = getpass.getpass("Paste the GitHub OAuth client secret (hidden), then Enter: ").strip()
    if not gh_secret:
        sys.exit("No GitHub client secret given; nothing deployed.")

    data = {
        "PLAID_CLIENT_ID": env["PLAID_CLIENT_ID"],
        "PLAID_SECRET": env["PLAID_SECRET"],
        "GITHUB_CLIENT_ID": GITHUB_CLIENT_ID,
        "GITHUB_CLIENT_SECRET": gh_secret,
        "COOKIE_ENCRYPTION_KEY": secrets.token_hex(32),
    }
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        print("Deploying to Cloudflare...")
        r = subprocess.run(f'npx wrangler deploy --secrets-file "{path}"', cwd=HERE, shell=True)
    finally:
        os.remove(path)
    print("\nDone." if r.returncode == 0 else f"\nDeploy failed (exit {r.returncode}).")


if __name__ == "__main__":
    main()
