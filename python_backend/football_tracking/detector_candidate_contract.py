from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from football_tracking.config import AppConfig
from football_tracking.tracking_contracts import (
    SCHEMA_VERSION,
    SOURCE_LINEAGE_FIELDS,
    TRACKING_CONTRACT_REPORT_NAME,
    build_tracking_contract,
)
from football_tracking.types import Candidate, OutputStatus, TrackResult

CANDIDATE_ID_VERSION = "v1"
CANDIDATE_ID_PREFIX = f"candidate-{CANDIDATE_ID_VERSION}-"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_CANDIDATE_ID_PATTERN = re.compile(r"candidate-v1-[0-9a-f]{16}-(?P<frame_index>[0-9]{9,})-[0-9a-f]{64}-[0-9]{4,}")


@dataclass(frozen=True, slots=True)
class CandidateSourceSnapshot:
    sha256: str
    stat_token: str | None
    resolved_path: Path | None
    is_mock: bool


class CandidateSourceChangedError(RuntimeError):
    pass


def validate_candidate_source_sha256(value: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError("candidate source sha256 must be 64 lowercase hexadecimal characters")
    return value


def validate_candidate_source_stat_token(value: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError("candidate source stat token must be 64 lowercase hexadecimal characters")
    return value


def capture_candidate_source_snapshot(config: AppConfig) -> CandidateSourceSnapshot:
    if config.mock.enabled:
        return CandidateSourceSnapshot(
            sha256=_mock_source_sha256(config),
            stat_token=None,
            resolved_path=None,
            is_mock=True,
        )

    video_path = Path(config.input_video).resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"Input video does not exist: {video_path}")
    source_sha256, stat_token = _sha256_file_snapshot(video_path)
    return CandidateSourceSnapshot(
        sha256=source_sha256,
        stat_token=stat_token,
        resolved_path=video_path,
        is_mock=False,
    )


def precomputed_candidate_source_snapshot(
    config: AppConfig,
    source_sha256: str,
    stat_token: str | None,
) -> CandidateSourceSnapshot:
    source_sha256 = validate_candidate_source_sha256(source_sha256)
    if config.mock.enabled:
        if stat_token is not None:
            raise ValueError("mock candidate sources cannot carry a filesystem stat token")
        return CandidateSourceSnapshot(source_sha256, None, None, True)
    if stat_token is None:
        raise ValueError("real-video candidate sources require a stat token")
    return CandidateSourceSnapshot(
        source_sha256,
        validate_candidate_source_stat_token(stat_token),
        Path(config.input_video).resolve(),
        False,
    )


def verify_candidate_source_snapshot(
    config: AppConfig,
    snapshot: CandidateSourceSnapshot,
    *,
    verify_content: bool,
) -> None:
    if snapshot.is_mock:
        if not config.mock.enabled or _mock_source_sha256(config) != snapshot.sha256:
            raise CandidateSourceChangedError("mock candidate source changed during tracking")
        return
    if config.mock.enabled or snapshot.resolved_path is None or snapshot.stat_token is None:
        raise CandidateSourceChangedError("candidate source kind changed during tracking")
    current_path = Path(config.input_video).resolve()
    if current_path != snapshot.resolved_path or not current_path.is_file():
        raise CandidateSourceChangedError("candidate source path changed during tracking")
    if verify_content:
        try:
            current_sha256, current_stat_token = _sha256_file_snapshot(current_path)
        except CandidateSourceChangedError:
            raise
        except OSError as exc:
            raise CandidateSourceChangedError("candidate source became unreadable during tracking") from exc
        if current_sha256 != snapshot.sha256 or current_stat_token != snapshot.stat_token:
            raise CandidateSourceChangedError("candidate source content changed during tracking")
        return
    try:
        current_stat_token = _stat_token(current_path.stat())
    except OSError as exc:
        raise CandidateSourceChangedError("candidate source became unreadable during tracking") from exc
    if current_stat_token != snapshot.stat_token:
        raise CandidateSourceChangedError("candidate source file identity changed during tracking")


def compute_candidate_source_sha256(
    config: AppConfig,
    *,
    use_precomputed: bool = True,
) -> str:
    """Return the content scope used by deterministic detector-candidate IDs."""

    precomputed = getattr(config.runtime, "candidate_source_sha256", None)
    if use_precomputed and precomputed is not None:
        return validate_candidate_source_sha256(precomputed)

    return capture_candidate_source_snapshot(config).sha256


def assign_candidate_ids(candidates: Iterable[Candidate], source_sha256: str) -> list[Candidate]:
    """Assign stable IDs without changing detector order or tracker behavior."""

    validate_candidate_source_sha256(source_sha256)

    ordered = list(candidates)
    identities = [(candidate.frame_index, _candidate_persisted_identity(candidate)) for candidate in ordered]
    for candidate, candidate_id in zip(ordered, _candidate_ids_from_identities(identities, source_sha256)):
        candidate.candidate_id = candidate_id
    return ordered


def validate_versioned_candidate_records(candidates: Iterable[dict[str, Any]], source_sha256: str) -> None:
    """Verify candidate-v1 rows against the exact mapped video content scope."""

    validate_candidate_source_sha256(source_sha256)
    records = list(candidates)
    versioned = [
        isinstance(record.get("candidate_id"), str) and record["candidate_id"].startswith(CANDIDATE_ID_PREFIX)
        for record in records
    ]
    if not any(versioned):
        return
    if not all(versioned):
        raise ValueError("candidate source mixes candidate-v1 and legacy candidate IDs")

    identities = [
        (
            record.get("frame_index"),
            _persisted_candidate_identity(
                frame_index=record.get("frame_index"),
                bbox=record.get("bbox"),
                confidence=record.get("confidence"),
                source=record.get("source"),
            ),
        )
        for record in records
    ]
    expected = sorted(_candidate_ids_from_identities(identities, source_sha256))
    actual = sorted(str(record["candidate_id"]) for record in records)
    if actual != expected:
        raise ValueError("candidate-v1 identity does not match its mapped video SHA-256 or detector evidence")


def candidate_to_contract_record(candidate: Candidate) -> dict[str, Any]:
    candidate_id = candidate.candidate_id
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ValueError("detector candidate is missing a deterministic candidate_id")
    match = _CANDIDATE_ID_PATTERN.fullmatch(candidate_id)
    if match is None or int(match.group("frame_index")) != candidate.frame_index:
        raise ValueError("detector candidate_id does not match the runtime candidate identity format")
    return {
        "candidate_id": candidate_id,
        "frame_index": candidate.frame_index,
        "bbox": [candidate.x1, candidate.y1, candidate.x2, candidate.y2],
        "confidence": candidate.confidence,
        "source": candidate.source,
    }


def track_result_to_contract_frame(track_result: TrackResult) -> dict[str, Any]:
    if track_result.output_status == OutputStatus.DETECTED:
        status = "detected"
    elif track_result.true_out_of_view_active or track_result.out_of_view_active:
        status = "out_of_view"
    elif track_result.output_status == OutputStatus.PREDICTED:
        status = "interpolated"
    else:
        status = "unknown"

    frame: dict[str, Any] = {
        "frame_index": track_result.frame_index,
        "status": status,
        "confidence": track_result.confidence,
        "source": "tracking_pipeline",
        "legacy_status": track_result.output_status.value,
    }
    if track_result.reason.strip():
        frame["reason"] = track_result.reason
    if track_result.point is not None:
        frame.update({"x": track_result.point.x, "y": track_result.point.y})
    return frame


class RuntimeTrackingContractWriter:
    """Bounded-memory, fail-closed writer for a normal detector run."""

    def __init__(
        self,
        output_dir: Path,
        source_sha256: str,
        *,
        source_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.source_sha256 = validate_candidate_source_sha256(source_sha256)
        self.source = _runtime_contract_source(self.source_sha256, source_metadata)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.final_path = self.output_dir / TRACKING_CONTRACT_REPORT_NAME
        token = uuid4().hex
        self.frames_path = self.output_dir / f".{TRACKING_CONTRACT_REPORT_NAME}.{token}.frames.jsonl"
        self.candidates_path = self.output_dir / f".{TRACKING_CONTRACT_REPORT_NAME}.{token}.candidates.jsonl"
        self.publish_path = self.output_dir / f".{TRACKING_CONTRACT_REPORT_NAME}.{token}.tmp"
        self.frame_count = 0
        self.candidate_count = 0
        self._last_frame_index: int | None = None
        self._failure: BaseException | None = None
        self._closed = False
        self._frames_file = None
        self._candidates_file = None
        self._output_lock = _acquire_contract_output_lock(self.output_dir)
        try:
            _cleanup_orphan_contract_spools(self.output_dir)
            _safe_unlink(self.final_path)
            self._frames_file = self.frames_path.open("x", encoding="utf-8")
            self._candidates_file = self.candidates_path.open("x", encoding="utf-8")
        except BaseException:
            _close_contract_spool_handles((self._frames_file, self._candidates_file))
            self._frames_file = None
            self._candidates_file = None
            for path in (self.frames_path, self.candidates_path, self.publish_path):
                try:
                    _safe_unlink(path)
                except BaseException:
                    pass
            try:
                _release_contract_output_lock(self._output_lock)
            except BaseException:
                pass
            self._output_lock = None
            raise

    def write(self, track_result: TrackResult) -> None:
        self._ensure_writable()

        try:
            frame = track_result_to_contract_frame(track_result)
            if self._last_frame_index is not None and track_result.frame_index <= self._last_frame_index:
                raise ValueError("runtime tracking contract frames must be written in strictly increasing order")
            if track_result.raw_candidate_count != len(track_result.raw_candidates):
                raise ValueError("raw_candidate_count does not match captured detector candidates")
            if any(candidate.frame_index != track_result.frame_index for candidate in track_result.raw_candidates):
                raise ValueError("runtime candidate frame_index must match its TrackResult frame_index")
            candidates = sorted(
                (candidate_to_contract_record(candidate) for candidate in track_result.raw_candidates),
                key=lambda item: item["candidate_id"],
            )
            self._write_contract_records(frame, candidates)
        except BaseException as exc:
            self._failure = exc
            raise

    def write_contract_records(
        self,
        frame: dict[str, Any],
        candidates: Iterable[dict[str, Any]],
    ) -> None:
        """Append one normalized frame and its source-scoped detector candidates."""

        self._ensure_writable()
        try:
            self._write_contract_records(frame, list(candidates))
        except BaseException as exc:
            self._failure = exc
            raise

    def _ensure_writable(self) -> None:
        if self._closed:
            raise RuntimeError("tracking contract writer is closed")
        if self._failure is not None:
            raise RuntimeError("tracking contract writer previously failed") from self._failure

    def _write_contract_records(
        self,
        frame: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> None:
        frame_index = frame.get("frame_index")
        if isinstance(frame_index, bool) or not isinstance(frame_index, int):
            raise ValueError("runtime tracking contract frame_index must be an integer")
        if self._last_frame_index is not None and frame_index <= self._last_frame_index:
            raise ValueError("runtime tracking contract frames must be written in strictly increasing order")
        if any(candidate.get("frame_index") != frame_index for candidate in candidates):
            raise ValueError("runtime candidate frame_index must match its contract frame_index")
        if any(
            not isinstance(candidate.get("candidate_id"), str)
            or not candidate["candidate_id"].startswith(CANDIDATE_ID_PREFIX)
            for candidate in candidates
        ):
            raise ValueError("runtime tracking contracts require candidate-v1 detector IDs")

        candidates.sort(key=lambda item: item["candidate_id"])
        validate_versioned_candidate_records(candidates, self.source_sha256)
        validated = build_tracking_contract(frames=[frame], candidates=candidates)
        if validated["validation_errors"]:
            raise ValueError(f"invalid runtime tracking contract records: {validated['validation_errors']}")
        if len(validated["frames"]) != 1 or len(validated["candidates"]) != len(candidates):
            raise ValueError("runtime tracking contract normalization changed the detector records")
        if self._frames_file is None or self._candidates_file is None:
            raise RuntimeError("tracking contract spool files are unavailable")

        _write_json_line(self._frames_file, validated["frames"][0])
        for candidate in validated["candidates"]:
            _write_json_line(self._candidates_file, candidate)
        self.frame_count += 1
        self.candidate_count += len(candidates)
        self._last_frame_index = frame_index

    def close(self, *, publish: bool) -> None:
        if self._closed:
            return
        self._closed = True

        failure = _close_contract_spool_handles((self._frames_file, self._candidates_file))
        self._frames_file = None
        self._candidates_file = None

        if failure is None and publish:
            try:
                if self._failure is None:
                    self._publish()
                else:
                    raise RuntimeError("tracking contract capture failed; no contract was published") from self._failure
            except BaseException as exc:
                failure = exc

        for path in (self.frames_path, self.candidates_path, self.publish_path):
            try:
                _safe_unlink(path)
            except BaseException as exc:
                if failure is None:
                    failure = exc

        if self._output_lock is not None:
            try:
                _release_contract_output_lock(self._output_lock)
            except BaseException as exc:
                if failure is None:
                    failure = exc
            finally:
                self._output_lock = None

        if failure is not None:
            raise failure

    def _publish(self) -> None:
        summary_status = "ok" if self.frame_count or self.candidate_count else "empty"
        summary = {
            "status": summary_status,
            "frame_count": self.frame_count,
            "candidate_count": self.candidate_count,
            "classification_count": 0,
            "decision_count": 0,
            "prelabel_count": 0,
            "confirmed_label_count": 0,
            "validation_error_count": 0,
        }
        with self.publish_path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write("{\n")
            handle.write(f'  "schema_version": {json.dumps(SCHEMA_VERSION)},\n')
            generated_at = datetime.now(timezone.utc).isoformat()
            handle.write(f'  "generated_at": {json.dumps(generated_at)},\n')
            handle.write(f'  "source": {json.dumps(self.source, separators=(",", ":"))},\n')
            handle.write(f'  "summary": {json.dumps(summary, separators=(",", ":"))},\n')
            handle.write('  "frames": [\n')
            _copy_jsonl_as_array(self.frames_path, handle)
            handle.write("  ],\n")
            handle.write('  "candidates": [\n')
            _copy_jsonl_as_array(self.candidates_path, handle)
            handle.write("  ],\n")
            handle.write('  "classifications": [],\n')
            handle.write('  "decisions": [],\n')
            handle.write('  "validation_errors": []\n')
            handle.write("}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(self.publish_path, self.final_path)


def _runtime_contract_source(source_sha256: str, metadata: dict[str, Any] | None) -> dict[str, Any]:
    source: dict[str, Any] = {"video_sha256": source_sha256}
    if metadata is None:
        return source
    unexpected = sorted(set(metadata) - (SOURCE_LINEAGE_FIELDS - {"video_sha256"}), key=str)
    if unexpected:
        raise ValueError(f"candidate source metadata contains unexpected fields: {unexpected}")
    for name in ("width", "height", "frame_count"):
        value = metadata.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"candidate source {name} must be a positive integer")
        source[name] = value
    if metadata.get("fps") is not None:
        fps = metadata["fps"]
        if isinstance(fps, bool):
            raise ValueError("candidate source fps must be positive and finite")
        try:
            parsed_fps = float(fps)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("candidate source fps must be positive and finite") from exc
        if not math.isfinite(parsed_fps) or parsed_fps <= 0.0:
            raise ValueError("candidate source fps must be positive and finite")
        source["fps"] = parsed_fps
    return source


def remove_runtime_tracking_contract(output_dir: Path) -> None:
    output_dir = Path(output_dir)
    output_lock = _acquire_contract_output_lock(output_dir)
    try:
        _cleanup_orphan_contract_spools(output_dir)
        _safe_unlink(output_dir / TRACKING_CONTRACT_REPORT_NAME)
    finally:
        _release_contract_output_lock(output_lock)


def _candidate_persisted_identity(candidate: Candidate) -> str:
    return _persisted_candidate_identity(
        frame_index=candidate.frame_index,
        bbox=(candidate.x1, candidate.y1, candidate.x2, candidate.y2),
        confidence=candidate.confidence,
        source=candidate.source,
    )


def _persisted_candidate_identity(
    *,
    frame_index: Any,
    bbox: Any,
    confidence: Any,
    source: Any,
) -> str:
    if isinstance(frame_index, bool) or not isinstance(frame_index, int) or frame_index < 0:
        raise ValueError("candidate frame_index must be a non-negative integer")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise ValueError("candidate bbox must contain four coordinates")
    if isinstance(confidence, bool):
        raise ValueError("candidate confidence must be between 0 and 1")
    try:
        parsed_bbox = tuple(float(value) for value in bbox)
        parsed_confidence = float(confidence)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("candidate coordinates and confidence must be finite") from exc
    values = (*parsed_bbox, parsed_confidence)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("candidate coordinates and confidence must be finite")
    if parsed_bbox[2] <= parsed_bbox[0] or parsed_bbox[3] <= parsed_bbox[1]:
        raise ValueError("candidate bbox max coordinates must exceed min coordinates")
    if not 0.0 <= parsed_confidence <= 1.0:
        raise ValueError("candidate confidence must be between 0 and 1")
    normalized_source = source.strip() if isinstance(source, str) else ""
    if not normalized_source:
        raise ValueError("candidate source must be a non-empty string")
    persisted = {
        "frame_index": frame_index,
        "bbox": [_canonical_float(value) for value in parsed_bbox],
        "confidence": _canonical_float(parsed_confidence),
        "source": normalized_source,
    }
    return json.dumps(persisted, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _candidate_ids_from_identities(identities: Iterable[tuple[int, str]], source_sha256: str) -> list[str]:
    occurrence_by_identity: dict[str, int] = {}
    result: list[str] = []
    for frame_index, identity in identities:
        occurrence = occurrence_by_identity.get(identity, 0)
        occurrence_by_identity[identity] = occurrence + 1
        identity_sha256 = hashlib.sha256(
            f"{CANDIDATE_ID_VERSION}\0{source_sha256}\0{identity}".encode("utf-8")
        ).hexdigest()
        result.append(f"{CANDIDATE_ID_PREFIX}{source_sha256[:16]}-{frame_index:09d}-{identity_sha256}-{occurrence:04d}")
    return result


def _canonical_float(value: float) -> str:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("candidate identity values must be finite")
    if parsed == 0.0:
        parsed = 0.0
    return parsed.hex()


def _mock_source_sha256(config: AppConfig) -> str:
    source = {
        "schema": "football_tracking.mock_source.v1",
        "scenario": config.mock.scenario.upper(),
        "frame_width": config.mock.frame_width,
        "frame_height": config.mock.frame_height,
        "fps": _canonical_float(config.mock.fps),
        "frame_count": config.mock.frame_count,
        "ball_box_size": config.mock.ball_box_size,
        "background_color": config.mock.background_color,
    }
    encoded = json.dumps(source, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_file_snapshot(path)[0]


def _sha256_file_snapshot(path: Path) -> tuple[str, str]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    current = path.stat()
    if _stat_signature(before) != _stat_signature(after) or _stat_signature(after) != _stat_signature(current):
        raise CandidateSourceChangedError(f"candidate source changed while hashing: {path}")
    return digest.hexdigest(), _stat_token(current)


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _stat_token(value: os.stat_result) -> str:
    encoded = json.dumps(_stat_signature(value), separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _write_json_line(handle: Any, payload: dict[str, Any]) -> None:
    handle.write(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")


def _copy_jsonl_as_array(path: Path, target: Any) -> None:
    first = True
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            record = line.strip()
            if not record:
                continue
            if not first:
                target.write(",\n")
            target.write(f"    {record}")
            first = False
    if not first:
        target.write("\n")


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        if path.exists():
            raise


def _close_contract_spool_handles(handles: Iterable[Any]) -> BaseException | None:
    failure: BaseException | None = None
    for handle in handles:
        if handle is None:
            continue
        try:
            handle.close()
        except BaseException as exc:
            if failure is None:
                failure = exc
            try:
                handle.close()
            except BaseException:
                pass
    return failure


def _acquire_contract_output_lock(output_dir: Path):
    lock_root = Path(tempfile.gettempdir()) / "football_tracking_contract_locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_scope = os.path.normcase(str(output_dir.resolve())).encode("utf-8")
    lock_path = lock_root / f"{hashlib.sha256(lock_scope).hexdigest()}.lock"
    handle = lock_path.open("a+b")
    try:
        handle.seek(0)
        if not handle.read(1):
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
    except BaseException as exc:
        handle.close()
        raise RuntimeError(f"another tracking contract writer is active in {output_dir}") from exc
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException as exc:
        handle.close()
        raise RuntimeError(f"another tracking contract writer is active in {output_dir}") from exc
    return handle


def _release_contract_output_lock(handle) -> None:
    failure: BaseException | None = None
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except BaseException as exc:
        failure = exc
    try:
        handle.close()
    except BaseException as exc:
        if failure is None:
            failure = exc
    if failure is not None:
        raise failure


def _cleanup_orphan_contract_spools(output_dir: Path) -> None:
    patterns = (
        f".{TRACKING_CONTRACT_REPORT_NAME}.*.frames.jsonl",
        f".{TRACKING_CONTRACT_REPORT_NAME}.*.candidates.jsonl",
        f".{TRACKING_CONTRACT_REPORT_NAME}.*.tmp",
    )
    for pattern in patterns:
        for path in output_dir.glob(pattern):
            _safe_unlink(path)
