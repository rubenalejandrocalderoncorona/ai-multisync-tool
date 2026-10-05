"""Roll a docs-site image out to its Deployment and check it. Runs as a Job under a service account that may only patch docs-qa and docs-prod.

  env: DEPLOY_ENV (qa|prod)   IMAGE (ghcr.io/<owner>/<repo>:<env>-<sha12>)   NAMESPACE

Standard library only. Exit code 1 when the rollout does not complete or the public URL does not answer 200.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
IMAGE_RE = re.compile(r"^ghcr\.io/[a-z0-9._/-]+:(qa|prod)-[0-9a-f]{12}$")
URLS = {"prod": "https://rubenalejandrocalderoncorona.org/documentation/", "qa": "https://rubenalejandrocalderoncorona.org/documentation/qa/"}


class Kube:
    def __init__(self, namespace: str):
        self.ns = namespace
        self.token = open(f"{SA}/token").read().strip()
        self.ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
        self.base = f"https://{os.environ.get('KUBERNETES_SERVICE_HOST', 'kubernetes.default.svc')}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}"

    def request(self, method: str, path: str, body=None, content_type="application/json") -> dict:
        req = urllib.request.Request(f"{self.base}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": content_type})
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=20) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"kubernetes API {e.code}: {e.read()[:300].decode(errors='replace')}") from e


def rolled_out(dep: dict) -> bool:
    st, spec = dep.get("status", {}), dep.get("spec", {})
    want = spec.get("replicas", 1)
    return (st.get("observedGeneration", 0) >= dep["metadata"]["generation"] and st.get("updatedReplicas", 0) == want
            and st.get("readyReplicas", 0) == want and st.get("replicas", 0) == want)


def deploy(kube, env_name: str, image: str, timeout: float = 180, poll: float = 3, sleep=time.sleep, fetch=None) -> str:
    """Patch the container image, wait for the rollout, then smoke-test the public URL. Returns a one-line summary."""
    if env_name not in ("qa", "prod"):
        raise ValueError("bad DEPLOY_ENV")
    if not IMAGE_RE.match(image) or not image.split(":")[1].startswith(env_name + "-"):
        raise ValueError(f"bad image for {env_name}: {image}")
    path = f"/apis/apps/v1/namespaces/{kube.ns}/deployments/docs-{env_name}"
    kube.request("PATCH", path, {"spec": {"template": {"spec": {"containers": [{"name": "nginx", "image": image}]}}}}, "application/strategic-merge-patch+json")
    print(f"patched docs-{env_name} -> {image}")
    deadline = time.time() + timeout
    while not rolled_out(kube.request("GET", path)):
        if time.time() > deadline:
            raise RuntimeError(f"docs-{env_name} did not finish rolling out in {timeout:.0f}s")
        sleep(poll)
    print(f"docs-{env_name} rolled out")
    url = URLS[env_name]
    fetch = fetch or (lambda u: urllib.request.urlopen(u, timeout=10).status)
    code = None
    for _ in range(12):
        try:
            code = fetch(url)
        except Exception as e:  # noqa: BLE001
            code = str(e)
        if code == 200:
            return f"{url} -> 200"
        sleep(5)
    raise RuntimeError(f"{url} did not return 200 (last: {code})")


def main() -> int:
    try:
        print(deploy(Kube(os.environ.get("NAMESPACE", "multirepo")), os.environ.get("DEPLOY_ENV", ""), os.environ.get("IMAGE", "")))
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
