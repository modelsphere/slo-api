"""In-memory index of LLMSLORequirement CRs, keyed by serviceId.

The public API routes by the opaque `route` string, which we resolve
against `spec.serviceId`. A CR is an addressable target iff it has a
non-empty serviceId and at least one namespace hosting it.

Two CRs in different namespaces may share a serviceId. Writes against
such routes are rejected 409 (we refuse to guess); reads return the
first-seen CR and log a warning.

The index is maintained by a background watch with capped exponential
backoff reconnect, mirroring decision_gen.SLOStore's pattern.
"""

import logging
import threading
import time

from kubernetes import client, watch

from slo_api import GROUP, VERSION, PLURAL

log = logging.getLogger(__name__)

_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 60.0


class CRIndex:
    def __init__(self, api=None, watch_factory=None):
        self._lock = threading.Lock()
        # (ns, name) -> full CR dict  (authoritative record)
        self._by_key = {}
        # service_id -> [(ns, name), ...]  in first-seen order
        self._by_service = {}

        self._api = api
        self._watch_factory = watch_factory
        self._stop = threading.Event()
        self._synced = threading.Event()
        self._thread = None
        self._started = False

    # ---------- lifecycle ----------

    def start(self):
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="cr-index",
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def synced(self):
        return self._synced.is_set()

    def wait_synced(self, timeout):
        return self._synced.wait(timeout)

    # ---------- lookups ----------

    def resolve(self, route):
        """Return (status, payload) for `route`.

        status:
          "ok"        — payload = (ns, name, cr)
          "ambiguous" — payload = [(ns, name), ...]
          "missing"   — payload = None
        """
        with self._lock:
            keys = self._by_service.get(route, [])
            if not keys:
                return "missing", None
            if len(keys) > 1:
                return "ambiguous", list(keys)
            ns, name = keys[0]
            cr = self._by_key.get((ns, name))
            if cr is None:
                # index said it existed but the record is gone — treat as missing
                return "missing", None
            return "ok", (ns, name, cr)

    def list_routes(self):
        """All currently indexed routes (serviceIds), sorted."""
        with self._lock:
            return sorted(self._by_service.keys())

    def all_crs(self):
        """Snapshot of indexed CRs: {(ns, name): cr}."""
        with self._lock:
            return dict(self._by_key)

    # ---------- watch loop ----------

    def _api_client(self):
        if self._api is None:
            self._api = client.CustomObjectsApi()
        return self._api

    def _run(self):
        backoff = _BACKOFF_INITIAL
        while not self._stop.is_set():
            try:
                self._watch_once()
                backoff = _BACKOFF_INITIAL
            except Exception:
                if self._stop.is_set():
                    break
                log.exception("cr-index watch error; retry in %.1fs", backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    def _watch_once(self):
        api = self._api_client()
        wf = self._watch_factory or watch.Watch
        w = wf()
        kwargs = {} if self._watch_factory else {"allow_watch_bookmarks": True}
        try:
            for event in w.stream(
                    api.list_cluster_custom_object,
                    GROUP, VERSION, PLURAL,
                    **kwargs):
                if self._stop.is_set():
                    return
                etype = event.get("type")
                cr = event.get("object") or {}
                if etype == "ERROR":
                    raise RuntimeError(f"watch ERROR event: {cr!r}")
                if etype == "BOOKMARK":
                    self._synced.set()
                    continue
                self._apply(etype, cr)
                self._synced.set()
        finally:
            stop = getattr(w, "stop", None)
            if callable(stop):
                stop()

    def _apply(self, etype, cr):
        meta = cr.get("metadata") or {}
        spec = cr.get("spec") or {}
        ns = meta.get("namespace")
        name = meta.get("name")
        if not ns or not name:
            return
        service_id = spec.get("serviceId") or ""

        with self._lock:
            if etype == "DELETED":
                self._by_key.pop((ns, name), None)
                lst = self._by_service.get(service_id)
                if lst and (ns, name) in lst:
                    lst.remove((ns, name))
                    if not lst:
                        self._by_service.pop(service_id, None)
                return

            # ADDED / MODIFIED
            prev = self._by_key.get((ns, name))
            prev_sid = (prev or {}).get("spec", {}).get("serviceId") or ""
            if prev_sid and prev_sid != service_id:
                # serviceId changed: detach from old key's index
                lst = self._by_service.get(prev_sid)
                if lst and (ns, name) in lst:
                    lst.remove((ns, name))
                    if not lst:
                        self._by_service.pop(prev_sid, None)

            self._by_key[(ns, name)] = cr
            lst = self._by_service.setdefault(service_id, [])
            if (ns, name) not in lst:
                lst.append((ns, name))
