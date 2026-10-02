# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Release tags carry
no leading `v`.

## [Unreleased]

### Added
- CI on every pull request: pytest and the end-to-end smoke run on Python 3.10
  and 3.12, a docker build, and a gate for the license text and committed
  credentials.
- Dependabot for the Python dependencies and the GitHub Actions.
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

[Unreleased]: https://github.com/modelsphere/slo-api/compare/0.2.0...HEAD
[0.2.0]: https://github.com/modelsphere/slo-api/compare/0.2.0-rc1...0.2.0
[0.2.0-rc1]: https://github.com/modelsphere/slo-api/compare/0.1.0...0.2.0-rc1
[0.1.0]: https://github.com/modelsphere/slo-api/releases/tag/0.1.0
