from __future__ import annotations

from contextlib import ExitStack, contextmanager
from typing import Iterator, Mapping
from unittest.mock import patch

import football_tracking.ball_annotation_service as annotation_service_module

_AUTHORITY_ALIASES = (
    "football_tracking.ball_annotation_service._AUDITED_T2_LEGACY_REPORT_BINDINGS",
    "football_tracking.ball_frame_evidence.AUDITED_T2_LEGACY_PROBE_BINDINGS",
    "football_tracking.api.schemas._AUDITED_T2_LEGACY_REPORT_BINDINGS",
    "football_tracking.detector_probe._AUDITED_T2_LEGACY_REPORT_BINDINGS",
)


@contextmanager
def patched_audited_t2_probe_bindings(
    additions: Mapping[str, Mapping[str, str]] | None = None,
) -> Iterator[dict[str, dict[str, str]]]:
    """Replace imported authority aliases without mutating the trust root."""

    bindings = {
        job_id: dict(binding)
        for job_id, binding in annotation_service_module._AUDITED_T2_LEGACY_REPORT_BINDINGS.items()
    }
    if additions:
        bindings.update({job_id: dict(binding) for job_id, binding in additions.items()})
    with ExitStack() as stack:
        for target in _AUTHORITY_ALIASES:
            stack.enter_context(patch(target, bindings))
        yield bindings


__all__ = ["patched_audited_t2_probe_bindings"]
