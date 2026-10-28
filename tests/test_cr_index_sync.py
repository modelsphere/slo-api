import threading

from slo_api.cr_index import CRIndex

from conftest import CRD_TEMPLATE, FakeCustomObjectsApi


class SilentWatch:
    """A watch the way a real API server answers one on an empty cluster: no ADDED events and no
    BOOKMARK for a long while. (FakeWatch in conftest always sends a BOOKMARK, which is why the
    tests never saw an index that could not get synced.)"""

    def __init__(self, stop):
        self._stop = stop

    def stream(self, fn, *args, **kwargs):
        while not self._stop.is_set():
            self._stop.wait(0.01)
        return
        yield  # pragma: no cover -- makes this a generator

    def stop(self):
        pass


def _index(api):
    idx = CRIndex(api=api, watch_factory=lambda: SilentWatch(idx._stop))
    return idx


def test_empty_cluster_is_synced():
    idx = _index(FakeCustomObjectsApi())
    idx.start()
    try:
        assert idx.wait_synced(timeout=2), "an empty list is complete: the index must report synced"
        assert idx.list_routes() == []
    finally:
        idx.stop()


def test_list_fills_the_index_before_any_watch_event():
    api = FakeCustomObjectsApi()
    api.seed(CRD_TEMPLATE("ns1", "a", "route-a"))
    api.seed(CRD_TEMPLATE("ns2", "b", "route-b"))
    idx = _index(api)
    idx.start()
    try:
        assert idx.wait_synced(timeout=2)
        assert idx.list_routes() == ["route-a", "route-b"]
    finally:
        idx.stop()
