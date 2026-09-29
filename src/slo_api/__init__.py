"""slo-api: HTTP facade over LLMSLORequirement CRs.

Exposes a route-keyed REST API and translates to/from the underlying
custom resource. Never creates new CRs; only get/patch/replace/delete
on existing ones.
"""
import logging as _logging
import os as _os

_log = _logging.getLogger("slo-api")

VERSION = "v1alpha1"
PLURAL = "llmslorequirements"

# The CRDs moved from inference.x-k8s.io to inference.modelsphere.dev. Both are
# listed so this runs against a cluster on either, in the order to prefer them:
# where both are served -- a migration in progress -- the new one wins, and the
# choice is made ONCE at startup rather than per call. Trying one group and
# falling back on failure would make every 404 ambiguous (absent resource, or
# wrong group?) and would have to be repeated in the watch loop, where a
# fallback re-lists the world.
GROUPS = ("inference.modelsphere.dev", "inference.x-k8s.io")
_group = GROUPS[0]


def group():
    """The group this process talks to. resolve_group() may change it once."""
    return _group


def resolve_group(apis_api=None, env=None, log=None):
    """Pick the group to use, once, at startup. Returns it.

    SLO_API_GROUP pins it and skips discovery -- the escape hatch for a cluster
    where the automatic answer is not the wanted one.

    Otherwise ask the API server which of GROUPS it serves and take the first
    in that order, so a cluster part-way through the migration is read under the
    new group. THAT CASE IS LOGGED AS A WARNING: while both are served, CRs can
    exist under either, and objects under the group not chosen are invisible to
    this process -- the same shape of silent-partial-answer this resolution
    exists to avoid. Pin the group when that matters.

    Discovery failing, or neither group being served, leaves the default: the
    first real request then reports the actual problem with the actual error.
    """
    global _group
    env = env if env is not None else _os.environ
    log = log or _log

    pinned = (env.get("SLO_API_GROUP") or "").strip()
    if pinned:
        _group = pinned
        log.info("LLMSLORequirement group pinned by SLO_API_GROUP: %s", _group)
        return _group

    try:
        from kubernetes import client as _client
        served = {g.name for g in (apis_api or _client.ApisApi()).get_api_versions().groups}
    except Exception as exc:
        log.warning("group discovery failed (%s); using %s", exc, _group)
        return _group

    present = [g for g in GROUPS if g in served]
    if not present:
        log.warning("cluster serves neither %s; using %s", " nor ".join(GROUPS), _group)
        return _group

    _group = present[0]
    if len(present) > 1:
        log.warning(
            "cluster serves both %s; using %s. CRs under the other group are "
            "invisible to slo-api -- set SLO_API_GROUP to pin it",
            " and ".join(present), _group)
    else:
        log.info("LLMSLORequirement group: %s", _group)
    return _group
    for g in GROUPS:
        if g in served:
            _group = g
            break
    return _group

HIGH_PRIORITY_VALUE = 10
LOW_PRIORITY_VALUE = 0
