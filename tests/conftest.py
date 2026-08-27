import copy
import threading

import pytest

from slo_api.cr_index import CRIndex


CRD_TEMPLATE = lambda ns, name, sid, extra_spec=None: {
    "apiVersion": "inference.x-k8s.io/v1alpha1",
    "kind": "LLMSLORequirement",
    "metadata": {"namespace": ns, "name": name, "resourceVersion": "1"},
    "spec": {"serviceId": sid, **(extra_spec or {})},
}


class FakeCustomObjectsApi:
    """In-memory CustomObjectsApi for tests."""

    def __init__(self):
        self._lock = threading.Lock()
        self._store = {}  # (ns, name) -> CR

    def seed(self, cr):
        ns = cr["metadata"]["namespace"]
        name = cr["metadata"]["name"]
        with self._lock:
            self._store[(ns, name)] = copy.deepcopy(cr)

    def list_cluster_custom_object(self, group, version, plural, **kw):
        with self._lock:
            items = [copy.deepcopy(c) for c in self._store.values()]
        return {"items": items}

    def get_namespaced_custom_object(self, group, version, ns, plural, name):
        with self._lock:
            cr = self._store.get((ns, name))
            if cr is None:
                from kubernetes.client import ApiException
                raise ApiException(status=404, reason="Not Found")
            return copy.deepcopy(cr)

    def patch_namespaced_custom_object(self, group, version, ns, plural, name, body):
        # JSON merge-patch (RFC 7386): null means delete the key.
        def merge(dst, src):
            for k, v in src.items():
                if v is None:
                    dst.pop(k, None)
                elif isinstance(v, dict) and isinstance(dst.get(k), dict):
                    merge(dst[k], v)
                else:
                    dst[k] = v

        with self._lock:
            cr = self._store.get((ns, name))
            if cr is None:
                from kubernetes.client import ApiException
                raise ApiException(status=404, reason="Not Found")
            merge(cr.setdefault("spec", {}), body.get("spec") or {})
            # bump resourceVersion like a real API server would
            rv = int(cr.get("metadata", {}).get("resourceVersion", "1")) + 1
            cr.setdefault("metadata", {})["resourceVersion"] = str(rv)
            return copy.deepcopy(cr)


class FakeWatch:
    """Yields each seeded CR as an ADDED event, then idles until stopped."""

    def __init__(self, api, outer_stop):
        self._api = api
        self._outer_stop = outer_stop

    def stream(self, fn, *args, **kwargs):
        import time
        for cr in self._api.list_cluster_custom_object("", "", "")["items"]:
            yield {"type": "ADDED", "object": cr}
        # BOOKMARK signals end-of-initial-list; index uses it to mark synced
        yield {"type": "BOOKMARK", "object": {}}
        while not self._outer_stop.is_set():
            time.sleep(0.01)

    def stop(self):
        pass


@pytest.fixture
def fake_api():
    return FakeCustomObjectsApi()


@pytest.fixture
def index(fake_api):
    idx = CRIndex(
        api=fake_api,
        watch_factory=lambda: FakeWatch(fake_api, idx._stop),
    )
    idx.start()
    assert idx.wait_synced(timeout=5)
    yield idx
    idx.stop()
