import base64
import json
import shutil
import subprocess

import httpx
import pytest

from multisync.cli import app_token

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")


def _key(tmp_path):
    p = tmp_path / "k.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(p), "2048"], check=True, capture_output=True)
    return p


def test_jwt_is_signed_and_verifies(tmp_path):
    k = _key(tmp_path)
    jwt = app_token.make_jwt("123", k.read_text(), now=1_000_000)
    head, body, sig = jwt.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    assert json.loads(base64.urlsafe_b64decode(pad(body))) == {"iat": 999_940, "exp": 1_000_540, "iss": "123"}
    pub = tmp_path / "pub.pem"
    subprocess.run(["openssl", "rsa", "-in", str(k), "-pubout", "-out", str(pub)], check=True, capture_output=True)
    (tmp_path / "sig").write_bytes(base64.urlsafe_b64decode(pad(sig)))
    r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(tmp_path / "sig")], input=f"{head}.{body}".encode(), capture_output=True)
    assert r.returncode == 0


def test_installation_token_flow(tmp_path):
    k = _key(tmp_path)
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path, req.headers["authorization"].startswith("Bearer ")))
        if req.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 77})
        return httpx.Response(201, json={"token": "ghs_test"})

    c = httpx.Client(transport=httpx.MockTransport(handler))
    assert app_token.installation_token("123", k.read_text(), "o/r", c) == "ghs_test"
    assert seen == [("GET", "/repos/o/r/installation", True), ("POST", "/app/installations/77/access_tokens", True)]
