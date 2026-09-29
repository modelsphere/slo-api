"""Entrypoint. Wires the CR watch and HTTP server."""

import logging
import os
import sys

from kubernetes import client, config as kube_config
from kubernetes.config.config_exception import ConfigException

import slo_api
from slo_api.cr_index import CRIndex
from slo_api.server import serve


def env_int(name, default):
    return int(os.environ.get(name, default))


def _configure_logging():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for name in ("kubernetes", "urllib3", "requests"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main():
    _configure_logging()
    log = logging.getLogger("slo_api")

    port = env_int("PORT", 8080)
    token = os.environ.get("SLO_API_TOKEN", "").strip()
    if not token:
        log.error("SLO_API_TOKEN env var is required")
        sys.exit(1)

    try:
        kube_config.load_incluster_config()
        log.info("kubernetes: using in-cluster config")
    except ConfigException:
        kube_config.load_kube_config()
        log.info("kubernetes: using local kubeconfig")

    slo_api.resolve_group()   # logs which group, and why

    api = client.CustomObjectsApi()
    index = CRIndex(api=api)

    try:
        index.start()
        if not index.wait_synced(timeout=30):
            log.warning("cr-index not synced after 30s; serving anyway")
        serve(port=port, index=index, api=api, auth_token=token)
    finally:
        index.stop()
        log.info("shutting down")


if __name__ == "__main__":
    main()
