package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strings"
	"testing"
)

func testCfg() *config {
	return &config{allowed: map[string]bool{"caimanlabs/calendar": true}, central: "me/docs", targetBr: "qa", namespace: "multirepo", image: "img"}
}

func sign(secret, body string) string {
	m := hmac.New(sha256.New, []byte(secret))
	m.Write([]byte(body))
	return "sha256=" + hex.EncodeToString(m.Sum(nil))
}

func TestSignature(t *testing.T) {
	body := []byte(`{"a":1}`)
	if !validSig([]byte("0123456789abcdef"), body, sign("0123456789abcdef", string(body))) {
		t.Fatal("valid signature rejected")
	}
	for _, h := range []string{"", "sha256=", "sha256=zz", sign("other-secret-value", string(body)), strings.Replace(sign("0123456789abcdef", string(body)), "sha256=", "sha1=", 1)} {
		if validSig([]byte("0123456789abcdef"), body, h) {
			t.Fatalf("accepted %q", h)
		}
	}
}

func parse(t *testing.T, s string) *payload {
	var p payload
	if err := json.Unmarshal([]byte(s), &p); err != nil {
		t.Fatal(err)
	}
	return &p
}

func TestPlanPush(t *testing.T) {
	c := testCfg()
	ok := `{"ref":"refs/heads/main","before":"` + strings.Repeat("a", 40) + `","after":"` + strings.Repeat("b", 40) + `","repository":{"full_name":"cAImanLabs/Calendar","default_branch":"main"},"commits":[{"added":["docs/a.md"],"modified":["README.md","../etc/passwd","x;rm -rf"],"removed":[]}]}`
	j, why := c.plan("push", parse(t, ok))
	if j == nil || j.Mode != "sync" || j.Target != "qa" || strings.Join(j.Files, ",") != "docs/a.md,README.md" {
		t.Fatalf("bad plan %+v %s", j, why)
	}
	for name, body := range map[string]string{
		"other repo":     strings.Replace(ok, "cAImanLabs/Calendar", "evil/repo", 1),
		"other branch":   strings.Replace(ok, "refs/heads/main", "refs/heads/dev", 1),
		"deleted branch": strings.Replace(ok, strings.Repeat("b", 40), strings.Repeat("0", 40), 1),
	} {
		if j, _ := c.plan("push", parse(t, body)); j != nil {
			t.Fatalf("%s should be ignored", name)
		}
	}
	if j, _ := c.plan("issues", parse(t, ok)); j != nil {
		t.Fatal("unknown event accepted")
	}
}

func TestPlanDispatchAndPR(t *testing.T) {
	c := testCfg()
	j, why := c.plan("repository_dispatch", parse(t, `{"client_payload":{"repository":"cAImanLabs/Calendar","sha":"abc1234","target_branch":"staging","changed_files":"docs/a.md docs/b.md"}}`))
	if j == nil || j.Target != "staging" || len(j.Files) != 2 {
		t.Fatalf("%+v %s", j, why)
	}
	if j, _ := c.plan("repository_dispatch", parse(t, `{"client_payload":{"repository":"cAImanLabs/Calendar","sha":"abc; id"}}`)); j != nil {
		t.Fatal("unsafe sha accepted")
	}
	pr := `{"action":"closed","repository":{"full_name":"me/docs"},"pull_request":{"merged":true,"merge_commit_sha":"abcdef1","head":{"ref":"docs-sync/x-abc"},"base":{"ref":"qa","sha":"1234567"}}}`
	j, why = c.plan("pull_request", parse(t, pr))
	if j == nil || j.Mode != "index" || j.MergeSHA != "abcdef1" {
		t.Fatalf("%+v %s", j, why)
	}
	if j, _ := c.plan("pull_request", parse(t, strings.Replace(pr, `"merged":true`, `"merged":false`, 1))); j != nil {
		t.Fatal("unmerged PR accepted")
	}
	if j, _ := c.plan("pull_request", parse(t, strings.Replace(pr, "docs-sync/", "feature/", 1))); j != nil {
		t.Fatal("non docs-sync PR accepted")
	}
}

func TestManifest(t *testing.T) {
	m := testCfg().jobManifest(&jobSpec{Mode: "sync", SourceRepo: "a/b", SHA: "ABCDEF123456", Target: "qa"})
	b, _ := json.Marshal(m)
	s := string(b)
	for _, want := range []string{`"ttlSecondsAfterFinished":300`, `"backoffLimit":1`, `"name":"multisync-sync-abcdef1-`, `"automountServiceAccountToken":false`} {
		if !strings.Contains(s, want) {
			t.Fatalf("missing %s in %s", want, s)
		}
	}
}
