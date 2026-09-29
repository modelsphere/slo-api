"""End-to-end handler tests: real ThreadingHTTPServer + fake k8s API."""

import json
import threading
import urllib.request
import urllib.error

import pytest

from slo_api.server import serve
from tests.conftest import CRD_TEMPLATE, FakeWatch
from slo_api.cr_index import CRIndex


TOKEN = "test-token"
BASE_CR_SPEC = {
    "serviceId": "kimi-k2.5",
    "priority": 10,
    "minimumDeployment": {"type": "replica", "value": 1},
    "maximumDeployment": {"type": "replica", "value": 2},
    "ttft": {"default": {"metrics": [{"type": "p80", "threshold": 20.0}]}},
    "otps": {"default": {"metrics": [{"type": "p80", "threshold": 30.0}]}},
}


@pytest.fixture
def running_server(fake_api):
    """Spin up the server bound to an ephemeral port."""
    from http.server import ThreadingHTTPServer
    from slo_api.server import make_handler

    idx_holder = {}
    def watch_factory():
        return FakeWatch(fake_api, idx_holder["idx"]._stop)

    idx = CRIndex(api=fake_api, watch_factory=watch_factory)
    idx_holder["idx"] = idx

    idx.start()
    assert idx.wait_synced(timeout=5)

    srv = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(idx, fake_api, TOKEN))
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    yield f"http://127.0.0.1:{port}", fake_api, idx

    srv.shutdown()
    srv.server_close()
    t.join(timeout=2)
    idx.stop()


def _req(method, url, body=None, token=TOKEN, headers=None):
    req = urllib.request.Request(url, method=method)
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None or headers:
        for k, v in (headers or {}).items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


# ---------- auth ----------

def test_no_auth_returns_401(running_server):
    url, _, _ = running_server
    st, body = _req("GET", f"{url}/config/kimi-k2.5", token=None)
    assert st == 401
    assert body["error"]["code"] == "unauthorized"


def test_wrong_token_returns_401(running_server):
    url, _, _ = running_server
    st, _ = _req("GET", f"{url}/config/kimi-k2.5", token="nope")
    assert st == 401


def test_healthz_no_auth_required(running_server):
    url, _, _ = running_server
    st, body = _req("GET", f"{url}/healthz", token=None)
    assert st == 200


def test_demo_web_ui_serves_index_html_without_auth(running_server):
    url, _, _ = running_server
    req = urllib.request.Request(f"{url}/demo-webui", method="GET")
    # Deliberately NO Authorization header — the page itself must be public.
    with urllib.request.urlopen(req) as r:
        body = r.read().decode()
        ct = r.headers.get("Content-Type", "")
    assert r.status == 200
    assert ct.startswith("text/html")
    assert "<title>slo-api</title>" in body


def test_root_path_not_served(running_server):
    """/ is intentionally not routed — API root only serves /demo-webui."""
    url, _, _ = running_server
    req = urllib.request.Request(f"{url}/", method="GET")
    # / with no auth → 401 (auth gate fires before route resolution)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 401


# ---------- GET ----------

def test_get_returns_full_config(running_server):
    url, api, idx = running_server
    spec = {k: v for k, v in BASE_CR_SPEC.items() if k != "serviceId"}
    cr = CRD_TEMPLATE("kimi", "kimi-k25", "kimi-k2.5", extra_spec=spec)
    api.seed(cr)
    idx._apply("ADDED", cr)

    st, body = _req("GET", f"{url}/config/kimi-k2.5")
    assert st == 200
    assert body["route"] == "kimi-k2.5"
    assert body["highPriority"] is True
    assert body["minimumDeployment"] == {"type": "replica", "value": 1}
    assert body["maximumDeployment"] == {"type": "replica", "value": 2}
    assert body["ttft"] == {"default": {"metrics": [{"type": "p80", "threshold": 20.0}]}}
    assert body["otps"] == {"default": {"metrics": [{"type": "p80", "threshold": 30.0}]}}


def test_get_unknown_route_returns_404(running_server):
    url, _, _ = running_server
    st, body = _req("GET", f"{url}/config/ghost")
    assert st == 404
    assert body["error"]["code"] == "not_found"


# ---------- PUT ----------

def test_put_on_missing_route_returns_404(running_server):
    url, _, _ = running_server
    st, body = _req("PUT", f"{url}/config/ghost",
                    body={"highPriority": True})
    assert st == 404


def test_put_validation_error_returns_400(running_server):
    url, api, idx = running_server
    cr = CRD_TEMPLATE("kimi", "kimi-k25", "kimi-k2.5")
    api.seed(cr)
    idx._apply("ADDED", cr)

    st, body = _req("PUT", f"{url}/config/kimi-k2.5",
                    body={"highPriority": "yes"})
    assert st == 400
    assert body["error"]["field"] == "highPriority"


def test_put_updates_priority_and_preserves_foreign_fields(running_server):
    url, api, idx = running_server
    cr = CRD_TEMPLATE("kimi", "kimi-k25", "kimi-k2.5", extra_spec={
        "priority": 0,
        "someForeignField": {"keep": "me"},
    })
    api.seed(cr)
    idx._apply("ADDED", cr)

    body = {
        "highPriority": True,
        "minimumDeployment": {"type": "replica", "value": 1},
        "maximumDeployment": {"type": "replica", "value": 2},
        "ttft": {"default": {"metrics": [{"type": "p80", "threshold": 20.0}]}},
        "otps": {"default": {"metrics": [{"type": "p80", "threshold": 30.0}]}},
    }
    st, resp = _req("PUT", f"{url}/config/kimi-k2.5", body=body)
    assert st == 200
    assert resp["route"] == "kimi-k2.5"
    assert resp["highPriority"] is True

    # Check the CR was patched and foreign fields preserved
    patched = api.get_namespaced_custom_object(
        "inference.modelsphere.dev", "v1alpha1", "kimi", "llmslorequirements", "kimi-k25")
    assert patched["spec"]["priority"] == 10
    assert patched["spec"]["someForeignField"] == {"keep": "me"}
    assert patched["spec"]["minimumDeployment"] == {"type": "replica", "value": 1}


def test_put_body_route_mismatch_returns_400(running_server):
    url, api, idx = running_server
    cr = CRD_TEMPLATE("kimi", "kimi-k25", "kimi-k2.5")
    api.seed(cr)
    idx._apply("ADDED", cr)

    st, body = _req("PUT", f"{url}/config/kimi-k2.5",
                    body={"route": "other", "highPriority": True})
    assert st == 400


def test_put_never_creates_cr(running_server):
    url, api, _ = running_server
    st, _ = _req("PUT", f"{url}/config/brand-new",
                 body={"highPriority": True})
    assert st == 404
    assert api.list_cluster_custom_object("", "", "")["items"] == []


# ---------- DELETE ----------

def test_delete_resets_slo_fields_keeps_cr(running_server):
    url, api, idx = running_server
    cr = CRD_TEMPLATE("kimi", "kimi-k25", "kimi-k2.5", extra_spec={
        "priority": 10,
        "ttft": {"default": {"metrics": [{"type": "p80", "threshold": 20.0}]}},
        "someForeignField": {"keep": "me"},
    })
    api.seed(cr)
    idx._apply("ADDED", cr)

    st, _ = _req("DELETE", f"{url}/config/kimi-k2.5")
    assert st == 204

    patched = api.get_namespaced_custom_object(
        "inference.modelsphere.dev", "v1alpha1", "kimi", "llmslorequirements", "kimi-k25")
    # Reset applies CRD defaults: priority and minimumDeployment have
    # explicit defaults in the schema; fields with no default become absent.
    assert patched["spec"]["priority"] == 0
    assert patched["spec"]["minimumDeployment"] == {"type": "replica", "value": 1}
    assert "maximumDeployment" not in patched["spec"]
    assert "ttft" not in patched["spec"]
    assert "otps" not in patched["spec"]
    # Everything else untouched
    assert patched["spec"]["someForeignField"] == {"keep": "me"}
    assert patched["spec"]["serviceId"] == "kimi-k2.5"


def test_delete_on_missing_route_returns_404(running_server):
    url, _, _ = running_server
    st, _ = _req("DELETE", f"{url}/config/ghost")
    assert st == 404


# ---------- LIST ----------

def test_list_empty(running_server):
    url, _, _ = running_server
    st, body = _req("GET", f"{url}/config")
    assert st == 200
    assert body == {"page": 1, "pageSize": 20, "total": 0, "items": []}


def test_list_pagination_and_summary_shape(running_server):
    url, api, idx = running_server
    for i in range(3):
        cr = CRD_TEMPLATE("kimi", f"m{i}", f"route-{i}", extra_spec={
            "priority": 10 if i == 0 else 0,
            "minimumDeployment": {"type": "replica", "value": i + 1},
            "maximumDeployment": {"type": "replica", "value": i + 5},
        })
        api.seed(cr)
        idx._apply("ADDED", cr)

    st, body = _req("GET", f"{url}/config?page=1&pageSize=2")
    assert st == 200
    assert body["total"] == 3
    assert body["pageSize"] == 2
    assert len(body["items"]) == 2

    # Summary should NOT include ttft/otps (per API doc)
    for item in body["items"]:
        assert "ttft" not in item
        assert "otps" not in item
        assert "route" in item
        assert "highPriority" in item

    st, body = _req("GET", f"{url}/config?page=2&pageSize=2")
    assert len(body["items"]) == 1


def test_list_invalid_page_params(running_server):
    url, _, _ = running_server
    st, _ = _req("GET", f"{url}/config?page=0")
    assert st == 400
    st, _ = _req("GET", f"{url}/config?pageSize=abc")
    assert st == 400
    st, _ = _req("GET", f"{url}/config?pageSize=9999")
    assert st == 400


# ---------- ambiguity ----------

def test_ambiguous_route_returns_409(running_server):
    url, api, idx = running_server
    cr1 = CRD_TEMPLATE("ns-a", "k25", "kimi-k2.5")
    cr2 = CRD_TEMPLATE("ns-b", "k25", "kimi-k2.5")
    api.seed(cr1); api.seed(cr2)
    idx._apply("ADDED", cr1); idx._apply("ADDED", cr2)

    st, body = _req("GET", f"{url}/config/kimi-k2.5")
    assert st == 409
    assert body["error"]["code"] == "conflict"

    st, _ = _req("PUT", f"{url}/config/kimi-k2.5",
                 body={"highPriority": True})
    assert st == 409
