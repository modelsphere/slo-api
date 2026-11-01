"""In-memory index of SLO requirement CRs (every kind in PLURALS), keyed by serviceId.

The public API routes by the opaque `route` string, which we resolve
against `spec.serviceId`. A CR is an addressable target iff it has a
non-empty serviceId and at least one namespace hosting it.

Two CRs may share a serviceId (different namespaces, or one of each
kind). Such routes are rejected 409 (we refuse to guess).

The index is maintained by one background watch per kind with capped
exponential backoff reconnect, mirroring decision_gen.SLOStore's pattern.
A kind whose CRD is not installed is indexed as empty.
"""

import logging
import threading
import time

from kubernetes import client, watch
from kubernetes.client import ApiException

from slo_api import VERSION, PLURALS, group

log = logging.getLogger(__name__)

_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 60.0


class CRIndex:
    def __init__(self, api=None, watch_factory=None):
        self._lock = threading.Lock()
        # (plural, ns, name) -> full CR dict  (authoritative record)
        self._by_key = {}
        # service_id -> [(plural, ns, name), ...]  in first-seen order
        self._by_service = {}

        self._api = api
        self._watch_factory = watch_factory
        self._stop = threading.Event()
        self._synced = {p: threading.Event() for p in PLURALS}
        self._threads = []
        self._started = False

    # ---------- lifecycle ----------

    def start(self):
        if self._started:
            return
        self._started = True
        for plural in PLURALS:
            t = threading.Thread(
                target=self._run, args=(plural,), daemon=True,
                name=f"cr-index-{plural}",
            )
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)

    def synced(self):
        return all(e.is_set() for e in self._synced.values())

    def wait_synced(self, timeout):
        deadline = time.monotonic() + timeout
        return all(e.wait(max(0.0, deadline - time.monotonic()))
                   for e in self._synced.values())

    # ---------- lookups ----------

    def resolve(self, route):
        """Return (status, payload) for `route`.

        status:
          "ok"        — payload = (plural, ns, name, cr)
          "ambiguous" — payload = [(plural, ns, name), ...]
          "missing"   — payload = None
        """
        with self._lock:
            keys = self._by_service.get(route, [])
            if not keys:
                return "missing", None
            if len(keys) > 1:
                return "ambiguous", list(keys)
            cr = self._by_key.get(keys[0])
            if cr is None:
                # index said it existed but the record is gone — treat as missing
                return "missing", None
            return "ok", (*keys[0], cr)

    def list_routes(self):
        """All currently indexed routes (serviceIds), sorted."""
        with self._lock:
            return sorted(self._by_service.keys())

    def all_crs(self):
        """Snapshot of indexed CRs: {(plural, ns, name): cr}."""
        with self._lock:
            return dict(self._by_key)

    # ---------- watch loop ----------

    def _api_client(self):
        if self._api is None:
            self._api = client.CustomObjectsApi()
        return self._api

    def _run(self, plural):
        backoff = _BACKOFF_INITIAL
        while not self._stop.is_set():
            try:
                self._watch_once(plural)
                backoff = _BACKOFF_INITIAL
            except Exception:
                if self._stop.is_set():
                    break
                log.exception("cr-index %s watch error; retry in %.1fs", plural, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    def _watch_once(self, plural):
        api = self._api_client()
        # List first, then watch from the list's resourceVersion. A bare watch only marks the index
        # synced on its first event, and with no CRs in the cluster there is none: the API server
        # sends no ADDED event and its first BOOKMARK can take minutes, so readiness stayed 503 and
        # the liveness probe killed the pod before it ever served. An empty list is a complete one.
        try:
            listing = api.list_cluster_custom_object(group(), VERSION, plural)
        except ApiException as e:
            if e.status != 404:
                raise
            # CRD not installed (JobSLORequirement is optional): no CRs of this kind is a complete
            # answer. Look again later, in case it gets installed.
            log.debug("cr-index: %s not served; retry in %.0fs", plural, _BACKOFF_MAX)
            self._replace(plural, [])
            self._synced[plural].set()
            self._stop.wait(_BACKOFF_MAX)
            return
        self._replace(plural, (listing or {}).get("items") or [])
        self._synced[plural].set()
        rv = ((listing or {}).get("metadata") or {}).get("resourceVersion")

        wf = self._watch_factory or watch.Watch
        w = wf()
        kwargs = {} if self._watch_factory else {"allow_watch_bookmarks": True}
        if rv and not self._watch_factory:
            kwargs["resource_version"] = rv
        try:
            for event in w.stream(
                    api.list_cluster_custom_object,
                    group(), VERSION, plural,
                    **kwargs):
                if self._stop.is_set():
                    return
                etype = event.get("type")
                cr = event.get("object") or {}
                if etype == "ERROR":
                    raise RuntimeError(f"watch ERROR event: {cr!r}")
                if etype == "BOOKMARK":
                    self._synced[plural].set()
                    continue
                self._apply(plural, etype, cr)
                self._synced[plural].set()
        finally:
            stop = getattr(w, "stop", None)
            if callable(stop):
                stop()

    def _replace(self, plural, items):
        """Rebuild one kind's part of the index from a full list: drops CRs deleted while no watch
        was running. Other kinds' entries are kept.

        Built aside and swapped in under the lock, so a reader sees the old index or the new one,
        never the empty or half-built one in between -- this runs on every watch reconnect."""
        fresh = {}
        for cr in items:
            meta = cr.get("metadata") or {}
            ns, name = meta.get("namespace"), meta.get("name")
            if ns and name:
                fresh[(plural, ns, name)] = cr
        with self._lock:
            by_key = {k: v for k, v in self._by_key.items() if k[0] != plural}
            by_key.update(fresh)
            by_service = {}
            for key, cr in by_key.items():
                sid = (cr.get("spec") or {}).get("serviceId") or ""
                by_service.setdefault(sid, []).append(key)
            self._by_key, self._by_service = by_key, by_service

    def _apply(self, plural, etype, cr):
        meta = cr.get("metadata") or {}
        spec = cr.get("spec") or {}
        ns = meta.get("namespace")
        name = meta.get("name")
        if not ns or not name:
            return
        service_id = spec.get("serviceId") or ""
        key = (plural, ns, name)

        with self._lock:
            if etype == "DELETED":
                self._by_key.pop(key, None)
                lst = self._by_service.get(service_id)
                if lst and key in lst:
                    lst.remove(key)
                    if not lst:
                        self._by_service.pop(service_id, None)
                return

            # ADDED / MODIFIED
            prev = self._by_key.get(key)
            prev_sid = (prev or {}).get("spec", {}).get("serviceId") or ""
            if prev_sid and prev_sid != service_id:
                # serviceId changed: detach from old key's index
                lst = self._by_service.get(prev_sid)
                if lst and key in lst:
                    lst.remove(key)
                    if not lst:
                        self._by_service.pop(prev_sid, None)

            self._by_key[key] = cr
            lst = self._by_service.setdefault(service_id, [])
            if key not in lst:
                lst.append(key)
