// Webhook receiver: verifies GitHub's HMAC signature and launches one ephemeral Kubernetes Job per accepted event.
// Standard library only, so the image is a single static binary (about 6 MB, a few MB of RAM while idle).
package main

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"regexp"
	"strings"
	"time"
)

const (
	saDir        = "/var/run/secrets/kubernetes.io/serviceaccount"
	maxBody      = 10 << 20
	maxFiles     = 200
	maxFilesSize = 16 << 10
)

var (
	repoRe   = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`)
	refRe    = regexp.MustCompile(`^[A-Za-z0-9_./-]+$`)
	shaRe    = regexp.MustCompile(`^[0-9a-f]{7,64}$`)
	fileRe   = regexp.MustCompile(`^[A-Za-z0-9_@+=,. /-]+$`)
	zeroSha  = regexp.MustCompile(`^0+$`)
	nameSafe = regexp.MustCompile(`[^a-z0-9]`)
)

type config struct {
	secret     []byte
	allowed    map[string]bool
	central    string
	targetBr   string
	namespace  string
	image      string
	pullPolicy string
	configMap  string
	secretName string
	token      string
	apiBase    string
	client     *http.Client
}

func env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func loadConfig() (*config, error) {
	c := &config{
		secret:     []byte(os.Getenv("WEBHOOK_SECRET")),
		allowed:    map[string]bool{},
		central:    env("CENTRAL_REPO", "rubenalejandrocalderoncorona/multirepo-agent-docs"),
		targetBr:   env("TARGET_BRANCH", "qa"),
		image:      env("JOB_IMAGE", "multisync-pipeline:local"),
		pullPolicy: env("JOB_IMAGE_PULL_POLICY", "IfNotPresent"),
		configMap:  env("JOB_CONFIGMAP", "multisync-config"),
		secretName: env("JOB_SECRET", "multisync-secrets"),
		apiBase:    "https://" + env("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc") + ":" + env("KUBERNETES_SERVICE_PORT", "443"),
	}
	if len(c.secret) < 16 {
		return nil, fmt.Errorf("WEBHOOK_SECRET must be set (at least 16 characters)")
	}
	for _, r := range strings.Split(os.Getenv("ALLOWED_REPOS"), ",") {
		if r = strings.TrimSpace(r); repoRe.MatchString(r) {
			c.allowed[strings.ToLower(r)] = true
		}
	}
	if len(c.allowed) == 0 {
		return nil, fmt.Errorf("ALLOWED_REPOS must list at least one org/repo")
	}
	ns, err := os.ReadFile(saDir + "/namespace")
	if err != nil {
		return nil, fmt.Errorf("not running in a cluster: %w", err)
	}
	c.namespace = strings.TrimSpace(string(ns))
	tok, err := os.ReadFile(saDir + "/token")
	if err != nil {
		return nil, err
	}
	c.token = strings.TrimSpace(string(tok))
	ca, err := os.ReadFile(saDir + "/ca.crt")
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(ca)
	c.client = &http.Client{Timeout: 15 * time.Second, Transport: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool}}}
	return c, nil
}

func validSig(secret, body []byte, header string) bool {
	const p = "sha256="
	if !strings.HasPrefix(header, p) {
		return false
	}
	got, err := hex.DecodeString(strings.TrimPrefix(header, p))
	if err != nil {
		return false
	}
	m := hmac.New(sha256.New, secret)
	m.Write(body)
	return hmac.Equal(m.Sum(nil), got)
}

// jobSpec is what the Job runs: mode "sync" (a source repo changed) or "index" (a docs PR was merged into the QA branch).
type jobSpec struct {
	Mode       string
	SourceRepo string
	SHA        string
	Before     string
	Target     string
	Files      []string
	BaseSHA    string
	MergeSHA   string
}

type payload struct {
	Ref        string `json:"ref"`
	Before     string `json:"before"`
	After      string `json:"after"`
	Deleted    bool   `json:"deleted"`
	Action     string `json:"action"`
	Repository struct {
		FullName      string `json:"full_name"`
		DefaultBranch string `json:"default_branch"`
	} `json:"repository"`
	Commits []struct {
		Added    []string `json:"added"`
		Modified []string `json:"modified"`
		Removed  []string `json:"removed"`
	} `json:"commits"`
	ClientPayload struct {
		Repository   string `json:"repository"`
		SHA          string `json:"sha"`
		Before       string `json:"before"`
		TargetBranch string `json:"target_branch"`
		ChangedFiles string `json:"changed_files"`
	} `json:"client_payload"`
	PullRequest struct {
		Merged         bool   `json:"merged"`
		MergeCommitSHA string `json:"merge_commit_sha"`
		Head           struct {
			Ref string `json:"ref"`
		} `json:"head"`
		Base struct {
			Ref string `json:"ref"`
			SHA string `json:"sha"`
		} `json:"base"`
	} `json:"pull_request"`
}

func cleanFiles(in []string) []string {
	seen := map[string]bool{}
	out := []string{}
	size := 0
	for _, f := range in {
		f = strings.TrimSpace(f)
		if f == "" || seen[f] || strings.Contains(f, "..") || strings.HasPrefix(f, "/") || !fileRe.MatchString(f) {
			continue
		}
		if len(out) >= maxFiles || size+len(f) > maxFilesSize {
			break
		}
		seen[f] = true
		size += len(f) + 1
		out = append(out, f)
	}
	return out
}

// plan turns a verified event into a job, or explains why the event is ignored (never an error for the sender).
func (c *config) plan(event string, p *payload) (*jobSpec, string) {
	switch event {
	case "ping":
		return nil, "pong"
	case "push":
		repo := strings.ToLower(p.Repository.FullName)
		if !c.allowed[repo] {
			return nil, "repository not allowed"
		}
		if p.Deleted || zeroSha.MatchString(p.After) || !shaRe.MatchString(p.After) {
			return nil, "branch deleted or invalid sha"
		}
		if p.Ref != "refs/heads/"+p.Repository.DefaultBranch {
			return nil, "not the default branch"
		}
		var files []string
		for _, cm := range p.Commits {
			files = append(files, cm.Added...)
			files = append(files, cm.Modified...)
		}
		before := p.Before
		if !shaRe.MatchString(before) || zeroSha.MatchString(before) {
			before = ""
		}
		return &jobSpec{Mode: "sync", SourceRepo: p.Repository.FullName, SHA: p.After, Before: before, Target: c.targetBr, Files: cleanFiles(files)}, ""
	case "repository_dispatch":
		cp := p.ClientPayload
		repo := cp.Repository
		if repo == "" {
			repo = p.Repository.FullName
		}
		if !c.allowed[strings.ToLower(repo)] || !repoRe.MatchString(repo) {
			return nil, "repository not allowed"
		}
		if !refRe.MatchString(cp.SHA) {
			return nil, "invalid sha"
		}
		target := cp.TargetBranch
		if target == "" {
			target = c.targetBr
		}
		if !refRe.MatchString(target) {
			return nil, "invalid target branch"
		}
		before := cp.Before
		if !shaRe.MatchString(before) {
			before = ""
		}
		return &jobSpec{Mode: "sync", SourceRepo: repo, SHA: cp.SHA, Before: before, Target: target, Files: cleanFiles(strings.Fields(cp.ChangedFiles))}, ""
	case "pull_request":
		if !strings.EqualFold(p.Repository.FullName, c.central) {
			return nil, "not the central repository"
		}
		pr := p.PullRequest
		if p.Action != "closed" || !pr.Merged || !strings.HasPrefix(pr.Head.Ref, "docs-sync/") || pr.Base.Ref != c.targetBr {
			return nil, "not a merged docs-sync PR into " + c.targetBr
		}
		if !shaRe.MatchString(pr.MergeCommitSHA) || !shaRe.MatchString(pr.Base.SHA) {
			return nil, "invalid shas"
		}
		return &jobSpec{Mode: "index", SourceRepo: p.Repository.FullName, SHA: pr.MergeCommitSHA, BaseSHA: pr.Base.SHA, MergeSHA: pr.MergeCommitSHA, Target: pr.Base.Ref}, ""
	}
	return nil, "event not handled"
}

func (c *config) jobManifest(j *jobSpec) map[string]any {
	short := nameSafe.ReplaceAllString(strings.ToLower(j.SHA), "")
	if len(short) > 7 {
		short = short[:7]
	}
	prefix := "multisync-sync-"
	if j.Mode == "index" {
		prefix = "multisync-index-"
	}
	name := fmt.Sprintf("%s%s-%d", prefix, short, time.Now().Unix())
	e := func(k, v string) map[string]any { return map[string]any{"name": k, "value": v} }
	envs := []any{
		e("JOB_MODE", j.Mode), e("SOURCE_REPO", j.SourceRepo), e("SOURCE_SHA", j.SHA), e("SOURCE_BEFORE", j.Before),
		e("TARGET_BRANCH", j.Target), e("CHANGED_FILES", strings.Join(j.Files, "\n")), e("CENTRAL_REPO", c.central),
		e("BASE_SHA", j.BaseSHA), e("MERGE_SHA", j.MergeSHA), e("QDRANT_URL", env("QDRANT_URL", "http://qdrant:6333")),
		e("HOME", "/work"),
	}
	return map[string]any{
		"apiVersion": "batch/v1", "kind": "Job",
		"metadata": map[string]any{"name": name, "namespace": c.namespace, "labels": map[string]any{"app": "multisync-job", "multisync/mode": j.Mode}},
		"spec": map[string]any{
			"ttlSecondsAfterFinished": 300, "backoffLimit": 1, "activeDeadlineSeconds": 1800,
			"template": map[string]any{
				"metadata": map[string]any{"labels": map[string]any{"app": "multisync-job"}},
				"spec": map[string]any{
					"restartPolicy": "Never", "automountServiceAccountToken": false,
					"securityContext": map[string]any{"runAsNonRoot": true, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000},
					"containers": []any{map[string]any{
						"name": "pipeline", "image": c.image, "imagePullPolicy": c.pullPolicy,
						"command": []any{"/app/scripts/job-entrypoint.sh"},
						"envFrom": []any{
							map[string]any{"configMapRef": map[string]any{"name": c.configMap, "optional": true}},
							map[string]any{"secretRef": map[string]any{"name": c.secretName}},
						},
						"env":             envs,
						"resources":       map[string]any{"requests": map[string]any{"cpu": "100m", "memory": "256Mi"}, "limits": map[string]any{"memory": "1Gi"}},
						"securityContext": map[string]any{"allowPrivilegeEscalation": false, "capabilities": map[string]any{"drop": []any{"ALL"}}},
						"volumeMounts":    []any{map[string]any{"name": "work", "mountPath": "/work"}},
						"workingDir":      "/work",
					}},
					"volumes": []any{map[string]any{"name": "work", "emptyDir": map[string]any{"sizeLimit": "2Gi"}}},
				},
			},
		},
	}
}

func (c *config) createJob(m map[string]any) (string, error) {
	b, _ := json.Marshal(m)
	req, _ := http.NewRequest("POST", c.apiBase+"/apis/batch/v1/namespaces/"+c.namespace+"/jobs", bytes.NewReader(b))
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", "application/json")
	res, err := c.client.Do(req)
	if err != nil {
		return "", err
	}
	defer res.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(res.Body, 1<<16))
	if res.StatusCode != http.StatusCreated {
		return "", fmt.Errorf("kubernetes API %d: %s", res.StatusCode, strings.TrimSpace(string(body)))
	}
	return m["metadata"].(map[string]any)["name"].(string), nil
}

func (c *config) handle(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBody))
	if err != nil {
		http.Error(w, "body too large", http.StatusRequestEntityTooLarge)
		return
	}
	if !validSig(c.secret, body, r.Header.Get("X-Hub-Signature-256")) {
		log.Printf("rejected: bad signature from %s", r.Header.Get("X-Forwarded-For"))
		http.Error(w, "invalid signature", http.StatusUnauthorized)
		return
	}
	var p payload
	if err := json.Unmarshal(body, &p); err != nil {
		http.Error(w, "invalid json", http.StatusBadRequest)
		return
	}
	event, delivery := r.Header.Get("X-GitHub-Event"), r.Header.Get("X-GitHub-Delivery")
	j, why := c.plan(event, &p)
	if j == nil {
		log.Printf("ignored event=%s delivery=%s: %s", event, delivery, why)
		w.WriteHeader(http.StatusOK)
		fmt.Fprintf(w, "ignored: %s\n", why)
		return
	}
	name, err := c.createJob(c.jobManifest(j))
	if err != nil {
		log.Printf("job creation failed event=%s delivery=%s: %v", event, delivery, err)
		http.Error(w, "could not start job", http.StatusBadGateway)
		return
	}
	log.Printf("started %s event=%s delivery=%s mode=%s repo=%s sha=%.7s", name, event, delivery, j.Mode, j.SourceRepo, j.SHA)
	w.WriteHeader(http.StatusAccepted)
	fmt.Fprintf(w, "job %s\n", name)
}

func main() {
	c, err := loadConfig()
	if err != nil {
		log.Fatal(err)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/sync-webhook", c.handle)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) { fmt.Fprint(w, "ok") })
	srv := &http.Server{Addr: ":8080", Handler: mux, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 20 * time.Second, WriteTimeout: 30 * time.Second}
	log.Printf("listening on :8080, allowed repos: %d, central: %s, image: %s", len(c.allowed), c.central, c.image)
	log.Fatal(srv.ListenAndServe())
}
