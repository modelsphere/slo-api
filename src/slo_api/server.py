"""HTTP surface for /v1/slo-config.

Routes (all require Bearer auth):
  PUT    /v1/slo-config/{route}   — full replace of SLO config on an existing CR
  GET    /v1/slo-config/{route}   — fetch current config
  DELETE /v1/slo-config/{route}   — reset SLO fields to defaults (CR is kept)
  GET    /v1/slo-config           — paginated list of configured routes

Never creates new CRs. PUT on a route that doesn't resolve to an
existing CR returns 404. If the route resolves to multiple CRs
(serviceId collision across namespaces), returns 409.
"""

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, unquote

from kubernetes.client import ApiException

from slo_api import PLURAL, GROUP, VERSION
from slo_api.translate import (
    ValidationError, api_to_spec, merge_spec, spec_to_api,
)

log = logging.getLogger(__name__)


def _error(status, code, message, field=None, details=None):
    body = {"error": {"code": code, "message": message}}
    if field:
        body["error"]["field"] = field
    if details:
        body["error"]["details"] = details
    return status, body


def make_handler(index, api, auth_token):
    """Factory. All three args dependency-injectable for tests."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "SLOApi/0.1"

        # ---------- plumbing ----------

        def log_message(self, format, *args):
            log.info("%s - %s", self.address_string(), format % args)

        def _respond(self, status, body=None):
            payload = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            request_id = self.headers.get("X-Request-Id")
            if request_id:
                self.send_header("X-Request-Id", request_id)
            if payload:
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
            else:
                self.send_header("Content-Length", "0")
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def _read_json(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None, _error(400, "invalid_request",
                                    "invalid Content-Length header")
            raw = self.rfile.read(n)
            try:
                return json.loads(raw.decode("utf-8")), None
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                return None, _error(400, "invalid_request",
                                    f"body is not valid JSON: {e}")

        def _check_auth(self):
            got = self.headers.get("Authorization") or ""
            if not got.startswith("Bearer "):
                return _error(401, "unauthorized",
                              "Authorization: Bearer <token> required")
            if got[len("Bearer "):] != auth_token:
                return _error(401, "unauthorized", "invalid token")
            return None

        # ---------- route ----------

        def _parse_route(self):
            path = unquote(urlparse(self.path).path)
            if path == "/v1/slo-config":
                return "list", None
            prefix = "/v1/slo-config/"
            if path.startswith(prefix) and len(path) > len(prefix):
                return "item", path[len(prefix):]
            return "other", None

        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/")
            if path in ("/healthz", "/health"):
                return self._respond(200, {"status": "ok"})
            if path == "/readyz":
                if index.synced():
                    return self._respond(200, {"status": "ok"})
                return self._respond(503, {"status": "not_ready",
                                           "reason": "cr watch not synced"})
            err = self._check_auth()
            if err: return self._respond(*err)
            kind, route = self._parse_route()
            if kind == "item":
                self._handle_get(route)
            elif kind == "list":
                params = parse_qs(urlparse(self.path).query)
                self._handle_list(params)
            else:
                self._respond(*_error(404, "not_found", "unknown path"))

        def do_PUT(self):
            err = self._check_auth()
            if err: return self._respond(*err)
            kind, route = self._parse_route()
            if kind != "item":
                return self._respond(*_error(404, "not_found", "unknown path"))
            self._handle_put(route)

        def do_DELETE(self):
            err = self._check_auth()
            if err: return self._respond(*err)
            kind, route = self._parse_route()
            if kind != "item":
                return self._respond(*_error(404, "not_found", "unknown path"))
            self._handle_delete(route)

        # ---------- route helpers ----------

        def _resolve(self, route):
            """(status_int, body_or_cr_tuple). On success → None, (ns, name, cr)."""
            status, payload = index.resolve(route)
            if status == "missing":
                return _error(404, "not_found",
                              f"route '{route}' not registered"), None
            if status == "ambiguous":
                return _error(
                    409, "conflict",
                    f"route '{route}' maps to multiple LLMSLORequirement CRs; "
                    "cannot auto-select a target",
                    details=[{"namespace": ns, "name": n}
                             for ns, n in payload]), None
            return None, payload

        # ---------- handlers ----------

        def _handle_get(self, route):
            err, target = self._resolve(route)
            if err: return self._respond(*err)
            ns, name, _ = target
            cr, gerr = self._fresh_get(ns, name, route)
            if gerr: return self._respond(*gerr)
            return self._respond(200, spec_to_api(route, cr.get("spec") or {}))

        def _fresh_get(self, ns, name, route):
            """Read the CR from the apiserver, bypassing the watch cache.
            Guarantees read-after-write consistency: callers that just
            modified the resource see their own writes. Returns
            (cr_dict, None) on success, (None, error_tuple) on failure."""
            try:
                cr = api.get_namespaced_custom_object(
                    GROUP, VERSION, ns, PLURAL, name)
                return cr, None
            except ApiException as e:
                if e.status == 404:
                    return None, _error(404, "not_found",
                                       f"CR for route '{route}' not found")
                log.warning("fresh get %s/%s failed: %s", ns, name, e.reason)
                return None, _error(502, "upstream_error",
                                    f"kubernetes get failed: {e.reason}")

        def _handle_put(self, route):
            err, target = self._resolve(route)
            if err: return self._respond(*err)
            ns, name, _ = target

            body, perr = self._read_json()
            if perr: return self._respond(*perr)
            # PUT is a full replace of the SLO config. Accept the body's
            # embedded `route` only if it matches the URL — otherwise 400.
            if "route" in body and body["route"] != route:
                return self._respond(*_error(
                    400, "invalid_request",
                    "body route does not match URL", field="route"))
            try:
                update = api_to_spec(body)
            except ValidationError as e:
                return self._respond(*_error(
                    400, "invalid_request", e.message, field=e.field))

            # Read the live CR (not the watch cache) so we merge over the
            # freshest state and never silently revert a concurrent write.
            cr, gerr = self._fresh_get(ns, name, route)
            if gerr: return self._respond(*gerr)
            existing = cr.get("spec") or {}
            merged = merge_spec(existing, update)
            merged["serviceId"] = existing.get("serviceId") or route

            try:
                api.patch_namespaced_custom_object(
                    GROUP, VERSION, ns, PLURAL, name,
                    {"spec": merged},
                )
            except ApiException as e:
                log.warning("patch %s/%s failed: %s %s", ns, name, e.status, e.reason)
                if e.status == 404:
                    return self._respond(*_error(
                        404, "not_found",
                        f"CR for route '{route}' disappeared during update"))
                return self._respond(*_error(
                    502, "upstream_error",
                    f"kubernetes patch failed: {e.reason}"))

            # Respond from the caller's intent, not the watch's view
            # (the watch may not have observed the change yet).
            return self._respond(200, spec_to_api(route, merged))

        def _handle_delete(self, route):
            err, target = self._resolve(route)
            if err: return self._respond(*err)
            ns, name, _ = target

            # Read the live CR (not the watch cache) so we know exactly
            # which SLO fields exist and need nulling out.
            cr, gerr = self._fresh_get(ns, name, route)
            if gerr: return self._respond(*gerr)

            # Reset SLO-owned fields to their CRD defaults. priority and
            # minimumDeployment have explicit CRD defaults; the other
            # three have none, so "reset" = absent. JSON merge-patch only
            # removes keys sent explicitly as null.
            patch = {"spec": {
                "priority": 0,
                "minimumDeployment": {"type": "replica", "value": 1},
                "maximumDeployment": None,
                "ttft": None,
                "otps": None,
            }}

            try:
                api.patch_namespaced_custom_object(
                    GROUP, VERSION, ns, PLURAL, name, patch,
                )
            except ApiException as e:
                log.warning("reset %s/%s failed: %s %s", ns, name, e.status, e.reason)
                if e.status == 404:
                    return self._respond(*_error(
                        404, "not_found",
                        f"CR for route '{route}' disappeared during delete"))
                return self._respond(*_error(
                    502, "upstream_error",
                    f"kubernetes patch failed: {e.reason}"))
            return self._respond(204, None)

        def _handle_list(self, params):
            try:
                page = int(params.get("page", ["1"])[0])
                page_size = int(params.get("pageSize", ["20"])[0])
            except ValueError:
                return self._respond(*_error(
                    400, "invalid_request",
                    "page and pageSize must be integers"))
            if page < 1 or page_size < 1 or page_size > 200:
                return self._respond(*_error(
                    400, "invalid_request",
                    "page must be >= 1 and pageSize in [1, 200]"))

            routes = index.list_routes()
            total = len(routes)
            slice_ = routes[(page - 1) * page_size: page * page_size]
            items = []
            for r in slice_:
                status, payload = index.resolve(r)
                if status != "ok":
                    # ambiguous routes are surfaced omitted from items,
                    # not 500'd — list offers a "what exists" view, not
                    # target resolution
                    continue
                _, _, cr = payload
                summary = spec_to_api(r, cr.get("spec") or {})
                summary.pop("ttft", None)
                summary.pop("otps", None)
                items.append(summary)

            return self._respond(200, {
                "page": page,
                "pageSize": page_size,
                "total": total,
                "items": items,
            })

    return Handler


def serve(port, index, api, auth_token):
    server = ThreadingHTTPServer(
        ("0.0.0.0", port), make_handler(index, api, auth_token))
    log.info("slo-api listening on :%d", port)
    server.serve_forever()
