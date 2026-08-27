"""Live smoke: start the real server against the fake k8s API and hit it
with curl-equivalent requests. Run standalone to validate the wire format.
"""

import json
import sys
import threading
import urllib.request
import urllib.error

sys.path.insert(0, "src")
sys.path.insert(0, "tests")

from conftest import CRD_TEMPLATE, FakeCustomObjectsApi, FakeWatch
from http.server import ThreadingHTTPServer
from slo_api.cr_index import CRIndex
from slo_api.server import make_handler


TOKEN = "test-token"


def req(method, url, body=None, token=TOKEN):
    r = urllib.request.Request(url, method=method)
    r.add_header("X-Request-Id", "smoke-req-123")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        r.data = json.dumps(body).encode()
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r) as resp:
            body = resp.read().decode()
            return resp.status, body, dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def step(label, method, url, body=None, expect=None, token=TOKEN):
    st, payload, hdrs = req(method, url, body=body, token=token)
    mark = "✓" if expect is None or st == expect else "✗ FAIL"
    print(f"{mark}  {label:55s}  → {st}")
    if payload and len(payload) < 300:
        print(f"    {payload}")
    if "X-Request-Id" in hdrs:
        print(f"    X-Request-Id: {hdrs['X-Request-Id']}")
    if expect and st != expect:
        raise SystemExit(f"expected {expect}, got {st}")
    return st, payload


def main():
    api = FakeCustomObjectsApi()
    cr = CRD_TEMPLATE("kimi", "kimi-k25", "kimi-k2.5", extra_spec={
        "priority": 0,
        "minimumDeployment": {"type": "replica", "value": 1},
        "maximumDeployment": {"type": "replica", "value": 2},
    })
    api.seed(cr)

    idx = CRIndex(api=api,
                  watch_factory=lambda: FakeWatch(api, idx_ref[0]._stop))
    idx_ref = [idx]
    idx.start()
    assert idx.wait_synced(timeout=5)

    srv = ThreadingHTTPServer(("127.0.0.1", 0),
                              make_handler(idx, api, TOKEN))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"server up on {base}\n")

    step("healthz (no auth)", "GET", f"{base}/healthz", token=None, expect=200)
    step("readyz (no auth)", "GET", f"{base}/readyz", token=None, expect=200)
    step("unauthenticated GET → 401", "GET",
         f"{base}/v1/slo-config/kimi-k2.5", token=None, expect=401)
    step("GET existing route", "GET",
         f"{base}/v1/slo-config/kimi-k2.5", expect=200)
    step("GET unknown route → 404", "GET",
         f"{base}/v1/slo-config/ghost", expect=404)

    put_body = {
        "highPriority": True,
        "minimumDeployment": {"type": "replica", "value": 1},
        "maximumDeployment": {"type": "replica", "value": 2},
        "ttft": {"default": {"metrics": [{"type": "p80", "threshold": 20.0,}]}},
        "otps": {"default": {"metrics": [{"type": "p80", "threshold": 30.0}]}},
    }
    step("PUT full config", "PUT", f"{base}/v1/slo-config/kimi-k2.5",
         body=put_body, expect=200)
    step("GET after PUT", "GET", f"{base}/v1/slo-config/kimi-k2.5", expect=200)
    step("PUT invalid (enum) → 400", "PUT",
         f"{base}/v1/slo-config/kimi-k2.5",
         body={"ttft": {"default": {"metrics": [{"type": "p99.9", "threshold": 1.0}]}}},
         expect=400)
    step("PUT unknown route → 404", "PUT",
         f"{base}/v1/slo-config/ghost", body={"highPriority": True}, expect=404)
    step("PUT route mismatch → 400", "PUT",
         f"{base}/v1/slo-config/kimi-k2.5",
         body={"route": "other"}, expect=400)

    step("LIST", "GET", f"{base}/v1/slo-config", expect=200)
    step("LIST with pagination", "GET",
         f"{base}/v1/slo-config?page=1&pageSize=1", expect=200)
    step("LIST bad page → 400", "GET",
         f"{base}/v1/slo-config?page=0", expect=400)

    step("DELETE", "DELETE", f"{base}/v1/slo-config/kimi-k2.5", expect=204)
    step("GET after DELETE (defaults)", "GET",
         f"{base}/v1/slo-config/kimi-k2.5", expect=200)
    step("DELETE unknown → 404", "DELETE",
         f"{base}/v1/slo-config/ghost", expect=404)

    # Verify the final CR state matches intent
    final = api.get_namespaced_custom_object(
        "inference.x-k8s.io", "v1alpha1", "kimi",
        "llmslorequirements", "kimi-k25")
    print(f"\nfinal CR spec: {json.dumps(final['spec'], indent=2)}")

    srv.shutdown()


if __name__ == "__main__":
    main()
