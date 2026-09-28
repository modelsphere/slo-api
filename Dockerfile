# The base image is a build arg so that a build behind a registry mirror, or on
# a runner that cannot reach Docker Hub, can point it at its own registry.
# See .gitlab-ci.yml for how the internal CI overrides it.
ARG BASE_IMAGE=python:3.12-slim

FROM ${BASE_IMAGE}

# Optional PyPI mirror, for the same reason. Empty (the default) uses PyPI.
#   docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple .
ARG PIP_INDEX_URL=""

WORKDIR /app
COPY pyproject.toml .
COPY src ./src

# A CI runner container can have a low thread limit, and pip's default progress
# bar starts a refresh thread, which fails the install with "can't start new
# thread". It has to be the environment variable, not the flag: the PEP 517
# build runs a nested pip for the build dependencies, and that child inherits
# only the environment.
ENV PIP_PROGRESS_BAR=off

RUN if [ -n "$PIP_INDEX_URL" ]; then \
        pip install --no-cache-dir -i "$PIP_INDEX_URL" . ; \
    else \
        pip install --no-cache-dir . ; \
    fi

EXPOSE 8080
USER 65534:65534

ENTRYPOINT ["python3", "-m", "slo_api"]
