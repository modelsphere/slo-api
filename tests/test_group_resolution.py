"""Which API group slo-api talks to, decided by what the cluster serves.

The rest of the suite cannot cover this: it asserts against the same constant
the code reads, so it agrees with whatever that constant says. These tests give
the resolver a cluster and check the group it picks, which is the only way a
wrong answer shows up as a failure.
"""
import slo_api


class _Group:
    def __init__(self, name):
        self.name = name


class _Versions:
    def __init__(self, names):
        self.groups = [_Group(n) for n in names]


class _Apis:
    """Stands in for kubernetes.client.ApisApi."""

    def __init__(self, names=(), raises=None):
        self._names = names
        self._raises = raises

    def get_api_versions(self):
        if self._raises:
            raise self._raises
        return _Versions(self._names)


def _restore():
    slo_api._group = slo_api.GROUPS[0]


def test_picks_the_new_group_when_only_it_is_served():
    _restore()
    assert slo_api.resolve_group(_Apis(["inference.modelsphere.dev", "apps"])) \
        == "inference.modelsphere.dev"
    assert slo_api.group() == "inference.modelsphere.dev"


def test_picks_the_old_group_when_that_is_what_the_cluster_has():
    _restore()
    assert slo_api.resolve_group(_Apis(["apps", "inference.x-k8s.io"])) \
        == "inference.x-k8s.io"
    assert slo_api.group() == "inference.x-k8s.io"


def test_prefers_the_new_group_while_both_exist():
    # A migration in progress. Ambiguity resolved by GROUPS order, not by
    # whichever the API server happens to list first.
    _restore()
    both = _Apis(["inference.x-k8s.io", "inference.modelsphere.dev"])
    assert slo_api.resolve_group(both) == "inference.modelsphere.dev"


def test_neither_group_served_leaves_the_default():
    _restore()
    assert slo_api.resolve_group(_Apis(["apps", "batch"])) == "inference.modelsphere.dev"


def test_unreachable_discovery_leaves_the_default():
    # Discovery failing is not a reason to refuse to start: the first real
    # request will report the actual problem, with the actual error.
    _restore()
    assert slo_api.resolve_group(_Apis(raises=RuntimeError("no cluster"))) \
        == "inference.modelsphere.dev"


def test_the_resolved_group_is_what_callers_use():
    # group() is read at call time. Were it imported by value, this would still
    # return the default and the resolution would be decorative.
    _restore()
    slo_api.resolve_group(_Apis(["inference.x-k8s.io"]))
    from slo_api import server, cr_index
    assert server.group() == "inference.x-k8s.io"
    assert cr_index.group() == "inference.x-k8s.io"
    _restore()


def _log():
    class L:
        def __init__(self):
            self.lines = {"info": [], "warning": []}

        def info(self, msg, *a):
            self.lines["info"].append(msg % a if a else msg)

        def warning(self, msg, *a):
            self.lines["warning"].append(msg % a if a else msg)
    return L()


def test_both_groups_served_warns_that_the_other_is_invisible():
    # The dangerous case: CRs can sit under either, and only one is read.
    # Silence here is what makes a partial answer look like a complete one.
    _restore()
    lg = _log()
    slo_api.resolve_group(_Apis(["inference.x-k8s.io", "inference.modelsphere.dev"]), log=lg)
    assert lg.lines["warning"], "picking between two served groups must warn"
    assert "invisible" in lg.lines["warning"][0]
    _restore()


def test_one_group_served_does_not_warn():
    # The control for the test above: without it, a warning on every start
    # would pass just as well and mean nothing.
    _restore()
    lg = _log()
    slo_api.resolve_group(_Apis(["inference.modelsphere.dev"]), log=lg)
    assert not lg.lines["warning"]
    _restore()
