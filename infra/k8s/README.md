# Infra

| Path | Purpose |
|---|---|
| `docker-compose.yml` | VPS stack: Qdrant + Postgres (FactStore), optional Caddy TLS edge, one-shot healthcheck |
| `postgres/init.sql` | FactStore schema (idempotent; also applied by `scripts/healthcheck.js`) |
| `k8s/` | Kustomize base mirroring the compose stack (StatefulSets + PVCs, probes, migrate Job) |

## Compose to Kubernetes mapping

| Compose | Kubernetes |
|---|---|
| `healthcheck` | `readinessProbe` / `livenessProbe` |
| named volume | `volumeClaimTemplates` |
| `.env` | `Secret` `multisync-secrets` |
| `pipeline` profile `tools` | `Job` `multisync-migrate` |
| `edge` profile | Ingress + cert-manager |

Validate the base without a cluster: `kubectl kustomize infra/k8s`.
