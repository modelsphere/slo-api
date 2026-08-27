import pytest

from slo_api.translate import (
    ValidationError, api_to_spec, merge_spec, spec_to_api,
)


# ---------- api_to_spec ----------

def test_high_priority_true_maps_to_10():
    spec = api_to_spec({"highPriority": True})
    assert spec["priority"] == 10


def test_high_priority_false_maps_to_0():
    spec = api_to_spec({"highPriority": False})
    assert spec["priority"] == 0


def test_high_priority_must_be_bool():
    with pytest.raises(ValidationError) as e:
        api_to_spec({"highPriority": "yes"})
    assert e.value.field == "highPriority"


def test_minimum_deployment_accepts_replica_and_concurrency():
    for t in ("replica", "concurrency"):
        spec = api_to_spec({"minimumDeployment": {"type": t, "value": 2}})
        assert spec["minimumDeployment"] == {"type": t, "value": 2}


def test_maximum_deployment_rejects_concurrency():
    # CRD schema restricts maximumDeployment.type to "replica"
    with pytest.raises(ValidationError) as e:
        api_to_spec({"maximumDeployment": {"type": "concurrency", "value": 2}})
    assert e.value.field == "maximumDeployment.type"


def test_deployment_value_must_be_positive_int():
    for bad in (0, -1, "two", 1.5, True):
        with pytest.raises(ValidationError):
            api_to_spec({"minimumDeployment": {"type": "replica", "value": bad}})


@pytest.mark.parametrize("mtype", ["avg", "p50", "p80", "p90", "p95", "p99"])
def test_metric_type_enum_ok(mtype):
    spec = api_to_spec({
        "ttft": {"default": {"metrics": [{"type": mtype, "threshold": 1.0}]}}
    })
    assert spec["ttft"]["default"]["metrics"][0]["type"] == mtype


def test_metric_type_rejects_unknown():
    with pytest.raises(ValidationError):
        api_to_spec({
            "ttft": {"default": {"metrics": [{"type": "p99.9", "threshold": 1.0}]}}
        })


def test_metric_threshold_must_be_numeric_and_non_negative():
    for bad in (-1.0, "high", True, None):
        with pytest.raises(ValidationError):
            api_to_spec({
                "ttft": {"default": {"metrics": [{"type": "p80", "threshold": bad}]}}
            })


def test_threshold_int_coerced_to_float():
    spec = api_to_spec({
        "ttft": {"default": {"metrics": [{"type": "p80", "threshold": 20}]}}
    })
    assert spec["ttft"]["default"]["metrics"][0]["threshold"] == 20.0


def test_default_metrics_required_when_section_present():
    with pytest.raises(ValidationError) as e:
        api_to_spec({"ttft": {"ranges": []}})
    assert e.value.field == "ttft.default"


def test_metrics_array_must_be_non_empty():
    with pytest.raises(ValidationError):
        api_to_spec({"ttft": {"default": {"metrics": []}}})


def test_ranges_full_shape():
    body = {
        "ttft": {
            "default": {"metrics": [{"type": "p80", "threshold": 20.0}]},
            "ranges": [{
                "contextLengthRangeLow": 4096,
                "contextLengthRangeHigh": 8192,
                "metrics": [{"type": "p95", "threshold": 30.0}]
            }]
        }
    }
    spec = api_to_spec(body)
    r = spec["ttft"]["ranges"][0]
    assert r["contextLengthRangeLow"] == 4096
    assert r["contextLengthRangeHigh"] == 8192
    assert r["metrics"][0]["type"] == "p95"


def test_range_high_optional():
    body = {
        "ttft": {
            "default": {"metrics": [{"type": "p80", "threshold": 20.0}]},
            "ranges": [{
                "contextLengthRangeLow": 0,
                "metrics": [{"type": "p95", "threshold": 30.0}]
            }]
        }
    }
    spec = api_to_spec(body)
    # key absent, not None — the CRD schema would reject explicit null
    assert "contextLengthRangeHigh" not in spec["ttft"]["ranges"][0]


def test_unknown_top_level_field_rejected():
    with pytest.raises(ValidationError) as e:
        api_to_spec({"highPriority": True, "rogueField": 1})
    assert "rogueField" in e.value.message


# ---------- merge_spec ----------

def test_merge_preserves_service_id_and_foreign_fields():
    existing = {
        "serviceId": "kimi-k2.5",
        "priority": 5,
        "someOtherField": {"owned": "by-elsewhere"},
    }
    update = api_to_spec({"highPriority": True})
    merged = merge_spec(existing, update)
    assert merged["serviceId"] == "kimi-k2.5"
    assert merged["priority"] == 10
    assert merged["someOtherField"] == {"owned": "by-elsewhere"}


# ---------- spec_to_api ----------

def test_spec_to_api_priority_10_to_true():
    out = spec_to_api("kimi", {"priority": 10, "serviceId": "kimi"})
    assert out["highPriority"] is True


@pytest.mark.parametrize("p", [0, 1, 5, 9])
def test_spec_to_api_priority_other_to_false(p):
    out = spec_to_api("kimi", {"priority": p, "serviceId": "kimi"})
    assert out["highPriority"] is False


def test_spec_to_api_roundtrip_full_body():
    body = {
        "highPriority": True,
        "minimumDeployment": {"type": "replica", "value": 1},
        "maximumDeployment": {"type": "replica", "value": 2},
        "ttft": {"default": {"metrics": [{"type": "p80", "threshold": 20.0}]}},
        "otps": {"default": {"metrics": [{"type": "p80", "threshold": 30.0}]}},
    }
    spec = api_to_spec(body)
    spec["serviceId"] = "kimi-k2.5"
    out = spec_to_api("kimi-k2.5", spec)
    assert out == {"route": "kimi-k2.5", **body}
