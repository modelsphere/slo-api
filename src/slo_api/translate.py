"""Translate between the public API shape and the SLO requirement CR spec.

API shape (what clients see):
  {
    "route": "kimi-k2.5",
    "highPriority": true,
    "minimumDeployment": {"type": "replica", "value": 1},
    "maximumDeployment": {"type": "replica", "value": 2},
    "ttft": {"default": {"metrics": [{"type": "p80", "threshold": 20.0}]},
             "ranges": [...]},
    "otps": {...}
  }

A JobSLORequirement route has `queue: {maxDepth, promql}` in place of
`ttft` / `otps`.

CRD shape uses `priority: int 0..10` and adds `serviceId`. Everything
else is structurally identical. `highPriority=true`  ⇆ priority==10
`highPriority=false` ⇆ priority in [0..9] (we write 0 on false).
"""

import copy

from slo_api import (
    HIGH_PRIORITY_VALUE, JOB_PLURAL, LLM_PLURAL, LOW_PRIORITY_VALUE,
)

# Fields the API is authoritative for — everything else in the CR spec
# is left untouched on write so out-of-band fields (owned by other
# controllers) survive a PUT.
SLO_FIELDS = (
    "priority",
    "minimumDeployment",
    "maximumDeployment",
    "ttft",
    "otps",
    "queue",
)

# Per kind, as its CRD schema has them: the SLO sections of the spec and the
# bound types. Cross-field rules (min/max types match, pdengine max != 1, ...)
# are CEL on the CRD, checked by the API server against the merged spec.
SECTIONS = {LLM_PLURAL: ("ttft", "otps"), JOB_PLURAL: ("queue",)}
ALLOWED_DEPLOY_TYPES_MIN = {LLM_PLURAL: {"replica", "concurrency", "pdengine"},
                            JOB_PLURAL: {"replica", "concurrency"}}
ALLOWED_DEPLOY_TYPES_MAX = {LLM_PLURAL: {"replica", "pdengine"},
                            JOB_PLURAL: {"replica"}}

ALLOWED_METRIC_TYPES = {"avg", "p50", "p80", "p90", "p95", "p99"}


class ValidationError(ValueError):
    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field
        self.message = message


# ---------- API → CRD ----------

def api_to_spec(body, plural=LLM_PLURAL):
    """Validate the API request body against the CR kind `plural` and
    return the SLO-owned slice of the CR spec. The caller is responsible
    for merging onto the existing spec (preserving foreign fields) or
    replacing wholesale.
    """
    if not isinstance(body, dict):
        raise ValidationError("request body must be a JSON object")

    spec = {}

    if "highPriority" in body:
        hp = body["highPriority"]
        if not isinstance(hp, bool):
            raise ValidationError("highPriority must be a boolean", "highPriority")
        spec["priority"] = HIGH_PRIORITY_VALUE if hp else LOW_PRIORITY_VALUE

    if "minimumDeployment" in body:
        spec["minimumDeployment"] = _validate_deployment(
            body["minimumDeployment"], "minimumDeployment",
            allowed_types=ALLOWED_DEPLOY_TYPES_MIN[plural],
        )
    if "maximumDeployment" in body:
        spec["maximumDeployment"] = _validate_deployment(
            body["maximumDeployment"], "maximumDeployment",
            allowed_types=ALLOWED_DEPLOY_TYPES_MAX[plural],
        )

    _reject_unknown_top_level(body, SECTIONS[plural])
    for key in SECTIONS[plural]:
        if key in body:
            validate = _validate_queue if key == "queue" else _validate_slo_section
            spec[key] = validate(body[key], key)
    return spec


def merge_spec(existing, update):
    """Return a new spec with SLO-owned fields from `update` applied on
    top of `existing`. Fields not present in `update` keep their
    existing value; foreign (non-SLO) fields in `existing` pass through
    untouched.
    """
    merged = copy.deepcopy(existing) if existing else {}
    for key in SLO_FIELDS:
        if key in update:
            merged[key] = copy.deepcopy(update[key])
    return merged


# ---------- CRD → API ----------

def spec_to_api(route, spec):
    """Project a CR spec into the public API shape."""
    out = {"route": route}
    if not spec:
        return out

    priority = spec.get("priority", LOW_PRIORITY_VALUE)
    out["highPriority"] = (priority == HIGH_PRIORITY_VALUE)

    for key in SLO_FIELDS[1:]:  # all but priority, translated above
        if key in spec:
            out[key] = copy.deepcopy(spec[key])
    return out


# ---------- validators ----------

def _reject_unknown_top_level(body, sections):
    allowed = {"route", "highPriority", "minimumDeployment",
               "maximumDeployment", *sections}
    unknown = set(body) - allowed
    if unknown:
        raise ValidationError(
            f"unknown field(s): {', '.join(sorted(unknown))}")


def _validate_deployment(value, field, allowed_types):
    if not isinstance(value, dict):
        raise ValidationError(f"{field} must be an object", field)
    dep_type = value.get("type", "replica")
    if dep_type not in allowed_types:
        raise ValidationError(
            f"{field}.type must be one of {sorted(allowed_types)}",
            f"{field}.type")
    dep_value = value.get("value")
    # 0 is scale-to-zero; the CRD's CEL ties it to a maximum of 0 too.
    if not isinstance(dep_value, int) or isinstance(dep_value, bool) or dep_value < 0:
        raise ValidationError(
            f"{field}.value must be an integer >= 0", f"{field}.value")
    return {"type": dep_type, "value": dep_value}


def _validate_queue(value, field):
    if not isinstance(value, dict):
        raise ValidationError(f"{field} must be an object", field)
    unknown = set(value) - {"maxDepth", "promql"}
    if unknown:
        raise ValidationError(
            f"{field}: unknown field(s): {', '.join(sorted(unknown))}",
            field)
    depth = value.get("maxDepth")
    if not isinstance(depth, int) or isinstance(depth, bool) or depth < 0:
        raise ValidationError(
            f"{field}.maxDepth must be an integer >= 0", f"{field}.maxDepth")
    out = {"maxDepth": depth}
    # Optional: the write is a merge patch, so a promql left out keeps the
    # CR's own (which the CRD requires).
    if "promql" in value:
        promql = value["promql"]
        if not isinstance(promql, str) or not promql.strip():
            raise ValidationError(
                f"{field}.promql must be a non-empty string", f"{field}.promql")
        out["promql"] = promql
    return out


def _validate_slo_section(value, field):
    if not isinstance(value, dict):
        raise ValidationError(f"{field} must be an object", field)
    unknown = set(value) - {"default", "ranges"}
    if unknown:
        raise ValidationError(
            f"{field}: unknown field(s): {', '.join(sorted(unknown))}",
            field)

    if "default" not in value:
        raise ValidationError(
            f"{field}.default is required when {field} is present",
            f"{field}.default")
    out = {"default": _validate_metric_group(
        value["default"], f"{field}.default")}

    if "ranges" in value:
        ranges = value["ranges"]
        if not isinstance(ranges, list):
            raise ValidationError(f"{field}.ranges must be an array",
                                  f"{field}.ranges")
        out["ranges"] = [
            _validate_range(r, f"{field}.ranges[{i}]")
            for i, r in enumerate(ranges)
        ]
    return out


def _validate_range(value, field):
    if not isinstance(value, dict):
        raise ValidationError(f"{field} must be an object", field)
    low = value.get("contextLengthRangeLow")
    high = value.get("contextLengthRangeHigh")
    if not isinstance(low, int) or isinstance(low, bool) or low < 0:
        raise ValidationError(
            f"{field}.contextLengthRangeLow must be an integer >= 0",
            f"{field}.contextLengthRangeLow")
    if high is not None and (
            not isinstance(high, int) or isinstance(high, bool) or high < 1):
        raise ValidationError(
            f"{field}.contextLengthRangeHigh must be an integer >= 1",
            f"{field}.contextLengthRangeHigh")
    if "metrics" not in value:
        raise ValidationError(f"{field}.metrics is required",
                              f"{field}.metrics")
    out = {
        "contextLengthRangeLow": low,
        "metrics": _validate_metrics(value["metrics"], f"{field}.metrics"),
    }
    if high is not None:
        out["contextLengthRangeHigh"] = high
    return out


def _validate_metric_group(value, field):
    if not isinstance(value, dict):
        raise ValidationError(f"{field} must be an object", field)
    if "metrics" not in value:
        raise ValidationError(f"{field}.metrics is required",
                              f"{field}.metrics")
    return {"metrics": _validate_metrics(value["metrics"], f"{field}.metrics")}


def _validate_metrics(value, field):
    if not isinstance(value, list) or not value:
        raise ValidationError(f"{field} must be a non-empty array", field)
    out = []
    for i, m in enumerate(value):
        mfield = f"{field}[{i}]"
        if not isinstance(m, dict):
            raise ValidationError(f"{mfield} must be an object", mfield)
        mtype = m.get("type")
        if mtype not in ALLOWED_METRIC_TYPES:
            raise ValidationError(
                f"{mfield}.type must be one of {sorted(ALLOWED_METRIC_TYPES)}",
                f"{mfield}.type")
        threshold = m.get("threshold")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) \
                or threshold < 0:
            raise ValidationError(
                f"{mfield}.threshold must be a number >= 0",
                f"{mfield}.threshold")
        out.append({"type": mtype, "threshold": float(threshold)})
    return out
