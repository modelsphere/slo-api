# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Release tags carry
no leading `v`.

## [Unreleased]

## [0.3.0] - 2026-11-02

### Added
- `JobSLORequirement` routes. The index watches both kinds, and a route
  resolves across them; a job route's body has `queue` (`maxDepth`, `promql`)
  in place of `ttft` / `otps`. A cluster without the JobSLO CRD serves the LLM
  routes as before. The RBAC in `config/rbac/` grants `jobslorequirements`.
- The `pdengine` bound type on `LLMSLORequirement` (helm-charts#95).
- Bound values of 0 (scale-to-zero), which the CRD has allowed since
  helm-charts#20; they were refused with `400` before.
- `docs/openapi.yaml`.

### Changed
- A write the CRD's rules reject (API server `422`) is `400 invalid_request`
  with the API server's message, not `502`.
- `GET /config` also lists job routes, in the same summary shape.

## [0.2.1] - 2026-10-28

### Fixed
- On a cluster with no `LLMSLORequirement`, slo-api never counted its index
  as synced and crash-looped on the liveness probe. The index now lists
  before it watches, so an empty list is a complete one.
- A watch reconnect cleared the index before rebuilding it, so a request in
  between answered a known route as unknown. The rebuilt index is swapped in
  whole.

### Added
- CI on every pull request: pytest and the end-to-end smoke run on Python 3.10
  and 3.12, a docker build, and a gate for the license text and committed
  credentials.
- `NOTICE`.

### Removed
- The internal GitLab pipeline (`.gitlab-ci.yml`).

## [0.2.0] - 2026-09-29

Everything in 0.2.0-rc1, except:

### Removed
- The `SLO_API_GROUP` override. The API group is always discovered; a cluster
  serving both groups is still logged as a warning naming both and the one in
  use.

## [0.2.0-rc1] - 2026-09-29

### Changed
- `LLMSLORequirement` is no longer hardcoded to `inference.x-k8s.io`. At
  startup slo-api asks the API server which of `inference.modelsphere.dev` and
  `inference.x-k8s.io` it serves and uses the first one present, the new group
  first. A cluster serving both is logged as a warning. `SLO_API_GROUP` pins
  the group and skips discovery.
- The RBAC in `config/rbac/` grants both groups.

## [0.1.0] - 2026-09-28

First tagged release.

### Added
- HTTP API over `LLMSLORequirement` custom resources, keyed by route
  (`spec.serviceId`): list, read, update and reset-to-defaults under
  `/config`, behind a Bearer token (`SLO_API_TOKEN`); `/healthz` and
  `/readyz`. It never creates or deletes a CR, and refuses writes with `409`
  when a route is ambiguous.
- `/demo-webui`: a static form for trying the API from a browser.
- Kubernetes manifests in `config/`: RBAC without create or delete, the
  Deployment and an example Secret.
- Dockerfile with the base image and PyPI index as build args, and a release
  workflow that builds `4pdosc/llm-slo-api` for linux/amd64 and linux/arm64
  from a version tag and pushes it to Docker Hub.
- Apache-2.0 `LICENSE`.

[Unreleased]: https://github.com/modelsphere/slo-api/compare/0.3.0...HEAD
[0.3.0]: https://github.com/modelsphere/slo-api/compare/0.2.1...0.3.0
[0.2.1]: https://github.com/modelsphere/slo-api/compare/0.2.0...0.2.1
[0.2.0]: https://github.com/modelsphere/slo-api/compare/0.2.0-rc1...0.2.0
[0.2.0-rc1]: https://github.com/modelsphere/slo-api/compare/0.1.0...0.2.0-rc1
[0.1.0]: https://github.com/modelsphere/slo-api/releases/tag/0.1.0
