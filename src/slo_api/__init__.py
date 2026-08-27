"""slo-api: HTTP facade over LLMSLORequirement CRs.

Exposes a route-keyed REST API and translates to/from the underlying
custom resource. Never creates new CRs; only get/patch/replace/delete
on existing ones.
"""

GROUP = "inference.x-k8s.io"
VERSION = "v1alpha1"
PLURAL = "llmslorequirements"

HIGH_PRIORITY_VALUE = 10
LOW_PRIORITY_VALUE = 0
