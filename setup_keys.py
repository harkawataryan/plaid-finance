"""Interactive setup: paste your Plaid keys and this writes them to .env, then checks they work.

Run:  .venv\\Scripts\\python setup_keys.py
The secret is read hidden (nothing shows while you paste) and is never printed.
"""
from __future__ import annotations

import getpass
from pathlib import Path

import httpx

from store import PLAID_HOSTS

ENV_PATH = Path(__file__).resolve().parent / ".env"


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def check_keys(env: str, client_id: str, secret: str) -> tuple[bool, str]:
    try:
        r = httpx.post(f"{PLAID_HOSTS[env]}/institutions/get", timeout=30, json={
            "client_id": client_id, "secret": secret, "count": 1, "offset": 0, "country_codes": ["US"],
        })
    except httpx.HTTPError as e:
        return False, f"could not reach Plaid: {e}"
    if r.status_code == 200:
        return True, "keys work"
    data = r.json()
    return False, f"{data.get('error_code')}: {data.get('error_message')}"


def main() -> None:
    current = read_env()
    print("Plaid key setup  (keys are at https://dashboard.plaid.com/developers/keys)\n")

    default_env = current.get("PLAID_ENV", "production")
    env = input(f"Environment - production (real accounts) or sandbox (fake data) [{default_env}]: ").strip().lower()
    env = env or default_env
    if env not in PLAID_HOSTS:
        raise SystemExit(f"'{env}' isn't valid; use production or sandbox.")

    old_id = current.get("PLAID_CLIENT_ID", "")
    hint = f" [keep {old_id[:6]}...]" if old_id else ""
    client_id = input(f"client_id{hint}: ").strip() or old_id

    secret = getpass.getpass(f"{env} secret (hidden; paste then press Enter, blank keeps current): ").strip()
    secret = secret or current.get("PLAID_SECRET", "")

    if not client_id or not secret:
        raise SystemExit("client_id and secret are both required.")

    print("\nChecking with Plaid...")
    ok, msg = check_keys(env, client_id, secret)
    print(("OK - " if ok else "Problem - ") + msg)
    if not ok and input("Save anyway? [y/N]: ").strip().lower() != "y":
        raise SystemExit("Nothing saved.")

    current.update({"PLAID_CLIENT_ID": client_id, "PLAID_SECRET": secret, "PLAID_ENV": env})
    ENV_PATH.write_text(
        "# Written by setup_keys.py - keep this file private (it is git-ignored)\n"
        + "".join(f"{k}={v}\n" for k, v in current.items()),
        encoding="utf-8",
    )
    print(f"\nSaved to {ENV_PATH}")
    print("Next: .venv\\Scripts\\python link_server.py   (to connect UFCU and Robinhood)")


if __name__ == "__main__":
    main()
