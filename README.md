# slo-api

A small HTTP API for reading and changing the SLO of an LLM inference service,
without handing the caller access to Kubernetes.

The SLO itself lives in an `LLMSLORequirement` custom resource, which
[slo-scaler-decision-gen](https://github.com/modelsphere/slo-scaler-decision-gen)
reads to decide replica counts. slo-api is a front door onto those resources:
callers address a service by its route name, send plain JSON, and never see a
namespace, a CR name or a kubeconfig.

The CRDs moved from `inference.x-k8s.io` to `inference.modelsphere.dev`, and
clusters are on either while that is in progress. slo-api asks the API server
which of the two it serves and uses that, deciding once at startup and logging
the answer. A cluster serving **both** is logged as a warning: CRs can exist
under either group while the migration is in progress, and only the chosen one
is read.

```
  client ── Bearer token ──▶ slo-api ──▶ LLMSLORequirement CRs ──▶ decision-gen
                              (patch only; never creates or deletes a CR)
```

## What it will and will not do

- **It never creates or deletes a CR.** A route must already have one; a `PUT`
  to an unknown route is `404`. The CR's lifecycle belongs to whatever deployed
  the service. The RBAC in `config/rbac/` grants exactly
  `get / list / watch / patch / update` and nothing else.
- **It only touches the SLO fields** — `priority`, `minimumDeployment`,
  `maximumDeployment`, `ttft`, `otps`. Anything else in the spec, written by
  other controllers, survives a write.
- **A route resolves through `spec.serviceId`.** If two namespaces have a CR with
  the same `serviceId`, writes are refused with `409` rather than guessing; reads
  return the first one seen.
- Writes merge onto the **live** CR, read fresh from the API server rather than
  from the watch cache, so a concurrent change is not silently reverted.

## API

Everything under `/config` needs `Authorization: Bearer <token>`.

| Method | Path | |
|---|---|---|
| GET | `/config?page=1&pageSize=20` | configured routes, paginated (`pageSize` ≤ 200) |
| GET | `/config/{route}` | one route's SLO |
| PUT | `/config/{route}` | set the fields in the body; fields left out keep their value |
| DELETE | `/config/{route}` | reset the SLO fields to the CRD defaults — the CR stays |
| GET | `/healthz`, `/readyz` | liveness; readiness waits for the CR watch to sync |
| GET | `/demo-webui` | a static form for trying the API from a browser (the page is public; its calls still need the token) |

The body is the CR spec with one translation — `priority` (0-10) is exposed as a
boolean:

```json
{
  "route": "my-model",
  "highPriority": true,
  "minimumDeployment": {"type": "replica", "value": 1},
  "maximumDeployment": {"type": "replica", "value": 2},
  "ttft": {"default": {"metrics": [{"type": "p80", "threshold": 20.0}]}},
  "otps": {"default": {"metrics": [{"type": "p80", "threshold": 30.0}]}}
}
```

`highPriority: true` writes `priority: 10`; `false` writes `0`. Metric types are
`avg`, `p50`, `p80`, `p90`, `p95`, `p99`. Errors come back as
`{"error": {"code", "message", "field"}}`, and an `X-Request-Id` header on the
request is echoed on the response.

## Running it

```bash
pip install -e '.[dev]'
python3 -m pytest -q          # no cluster needed: the Kubernetes API is faked
SLO_API_TOKEN=... slo-api     # against your current kubeconfig
```

In a cluster (the manifests assume a `llm-scaler` namespace):

```bash
kubectl apply -f config/rbac/rbac.yaml
cp config/manager/secret.example.yaml config/manager/secret.yaml   # set the token; this file is git-ignored
kubectl apply -f config/manager/secret.yaml -f config/manager/manager.yaml
```

The [`llm-slo-decision-gen`](https://github.com/modelsphere/helm-charts) Helm
chart deploys it together with decision-gen.

| Variable | Default | |
|---|---|---|
| `SLO_API_TOKEN` | — | required; the process exits without it |
| `PORT` | `8080` | |
| `LOG_LEVEL` | `INFO` | |

## Building the image

```bash
docker build -t llm-slo-api .
```

The base image and the PyPI index are build args, for builds that cannot reach
Docker Hub or PyPI:

```bash
docker build \
  --build-arg BASE_IMAGE=my-registry/python:3.12-slim \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple .
```

Released images are `4pdosc/llm-slo-api` on Docker Hub, built from version tags
by `.github/workflows/release.yml`.

## License

Apache-2.0, see [LICENSE](LICENSE).
