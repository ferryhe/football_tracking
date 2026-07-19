from __future__ import annotations

from types import MappingProxyType
from typing import Final, Mapping

# These values are copied from the independently audited T2 evidence report in
# docs/operations/detector-probe-real-video-evidence.md.  Keep one shared trust
# root: detector validation, annotation production, API schemas, and offline
# package verification must never carry divergent private copies.
_AUDITED_T2_LEGACY_PROBE_BINDINGS = {
    "probe-308adcc1feaa99cc-bddecb26d127": {
        "canonical_job_record_sha256": "9fc18e56adbfc1cb4577746d807494e533aa5fbea2ce45b5fe4e05e3fc8a25df",
        "request_sha256": "308adcc1feaa99cc818ed05abba3d960ffe5a724439d2b86eddcc1b823954eb1",
        "report_sha256": "9f422a6b0270e7e4e933505949cab5aabc32eaa0d4e36ebe28a269bdcf083644",
        "result_manifest_sha256": "cee7fe623740356457d40493d45ba98fdd109138de14bb7d73bbeeabb60bcbee",
        "execution_bundle_sha256": "fe8ffe2aca1d83f5b3ed19d6fff96999fa3224d3688147c23e1622fe58947494",
        "runtime_environment_sha256": "6ae75ef83b968801c2c0f16a628e9eba94fe88aed27384c2896d08c7e90054d3",
    },
    "probe-b59e904ee0b8a14f-e2aee08f414e": {
        "canonical_job_record_sha256": "5032ed68a95659a2321255e3815dc49d6b755260026870d3ed1e0b390c6edc8b",
        "request_sha256": "b59e904ee0b8a14fd8ee61d4d26909b2c4c08be8d2d8787687d19e3f6b2ca293",
        "report_sha256": "bc339d742993075849a0b892d90e36800ea7c4bcab1be0349b601056bfc67bcc",
        "result_manifest_sha256": "78258e7078c7892814891f7e5797623dfc778c601613c0ea67a7609b79af81b6",
        "execution_bundle_sha256": "fe8ffe2aca1d83f5b3ed19d6fff96999fa3224d3688147c23e1622fe58947494",
        "runtime_environment_sha256": "6ae75ef83b968801c2c0f16a628e9eba94fe88aed27384c2896d08c7e90054d3",
    },
}

AUDITED_T2_LEGACY_PROBE_BINDINGS: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {job_id: MappingProxyType(dict(binding)) for job_id, binding in _AUDITED_T2_LEGACY_PROBE_BINDINGS.items()}
)
del _AUDITED_T2_LEGACY_PROBE_BINDINGS


__all__ = ["AUDITED_T2_LEGACY_PROBE_BINDINGS"]
