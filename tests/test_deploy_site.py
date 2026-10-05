import pytest

from multisync.cli.deploy_site import deploy, rolled_out

IMG = "ghcr.io/me/docs:qa-" + "a" * 12


class FakeKube:
    ns = "multirepo"

    def __init__(self, ready_after=2):
        self.calls, self.gets, self.ready_after = [], 0, ready_after

    def request(self, method, path, body=None, content_type="application/json"):
        self.calls.append((method, path, body, content_type))
        if method == "GET":
            self.gets += 1
            ok = self.gets >= self.ready_after
            return {"metadata": {"generation": 5}, "spec": {"replicas": 1}, "status": {"observedGeneration": 5, "updatedReplicas": 1 if ok else 0, "readyReplicas": 1 if ok else 0, "replicas": 1}}
        return {}


def test_deploy_patches_the_image_waits_for_the_rollout_and_smoke_tests():
    k = FakeKube()
    out = deploy(k, "qa", IMG, sleep=lambda s: None, fetch=lambda u: 200)
    assert out.endswith("/documentation/qa/ -> 200")
    method, path, body, ctype = k.calls[0]
    assert (method, path) == ("PATCH", "/apis/apps/v1/namespaces/multirepo/deployments/docs-qa")
    assert body["spec"]["template"]["spec"]["containers"] == [{"name": "nginx", "image": IMG}]
    assert ctype == "application/strategic-merge-patch+json"


def test_deploy_rejects_foreign_or_mismatched_images_before_touching_the_cluster():
    k = FakeKube()
    for env, img in [("qa", "evil.io/x:qa-" + "a" * 12), ("prod", IMG), ("qa", "ghcr.io/me/docs:latest"), ("dev", IMG)]:
        with pytest.raises(ValueError):
            deploy(k, env, img, sleep=lambda s: None)
    assert k.calls == []


def test_deploy_fails_when_the_public_url_never_answers_200():
    with pytest.raises(RuntimeError, match="did not return 200"):
        deploy(FakeKube(1), "qa", IMG, sleep=lambda s: None, fetch=lambda u: 502)


def test_rolled_out_needs_the_new_generation_and_all_replicas_ready():
    dep = {"metadata": {"generation": 2}, "spec": {"replicas": 1}, "status": {"observedGeneration": 1, "updatedReplicas": 1, "readyReplicas": 1, "replicas": 1}}
    assert not rolled_out(dep)
    dep["status"]["observedGeneration"] = 2
    assert rolled_out(dep)
