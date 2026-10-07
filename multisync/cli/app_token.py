"""Print a short-lived GitHub App installation token for CENTRAL_REPO, so review PRs are authored by the bot and a human can approve them.

env: BOT_APP_ID  BOT_APP_PRIVATE_KEY (PEM)  CENTRAL_REPO
The JWT is signed with the openssl CLI (no extra Python dependency). Prints nothing and exits 1 when anything is missing or fails,
so the Job can fall back to the PAT and say so.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time

import httpx

API = "https://api.github.com"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(app_id: str, pem: str, now: int | None = None) -> str:
    now = int(time.time()) if now is None else now
    head = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    body = b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode())
    signing_input = f"{head}.{body}".encode()
    with tempfile.NamedTemporaryFile("w", suffix=".pem") as f:
        os.chmod(f.name, 0o600)
        f.write(pem if pem.endswith("\n") else pem + "\n")
        f.flush()
        sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", f.name], input=signing_input, capture_output=True, check=True).stdout
    return f"{head}.{body}.{b64url(sig)}"


def installation_token(app_id: str, pem: str, repo: str, client: httpx.Client | None = None) -> str:
    c = client or httpx.Client(timeout=30)
    h = {"Authorization": f"Bearer {make_jwt(app_id, pem)}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    inst = c.get(f"{API}/repos/{repo}/installation", headers=h)
    inst.raise_for_status()
    tok = c.post(f"{API}/app/installations/{inst.json()['id']}/access_tokens", headers=h)
    tok.raise_for_status()
    return tok.json()["token"]


def main() -> int:
    env = os.environ
    try:
        print(installation_token(env["BOT_APP_ID"], env["BOT_APP_PRIVATE_KEY"], env["CENTRAL_REPO"]))
    except Exception as e:  # noqa: BLE001
        print(f"no bot token: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
