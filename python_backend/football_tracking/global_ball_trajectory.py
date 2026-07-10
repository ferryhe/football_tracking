from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from functools import partial
from itertools import groupby
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator
from uuid import uuid4

import cv2
import ijson  # pyright: ignore[reportMissingImports]

from football_tracking.kalman import ConstantAccelerationKalmanFilter
from football_tracking.tracking_contracts import (
    CLASSIFICATION_LABELS,
    FRAME_STATUSES,
    LABEL_ORIGINS,
    LEGACY_STATUS_MAP,
    SOURCE_LINEAGE_FIELDS,
)
from football_tracking.tracking_contracts import (
    SCHEMA_VERSION as TRACKING_CONTRACT_SCHEMA_VERSION,
)

TRACK_NAME = "ball_track.v2.csv"
DECISIONS_NAME = "global_ball_trajectory_decisions.v1.jsonl"
REPORT_NAME = "global_ball_trajectory_report.v1.json"
REPORT_SCHEMA_VERSION = "1.0"
ALGORITHM_VERSION = "global-ball-trajectory-v1"

_SHA256_LENGTH = 64
_COPY_CHUNK_BYTES = 1024 * 1024
_PROBABILITY_TOLERANCE = 1e-5
_JSON_MAX_VALUE_TOKENS = 4096
_JSON_MAX_DEPTH = 16
_JSON_MAX_STRING_CHARS = 65536
_JSON_MAX_CONTAINER_ITEMS = 1024
_CANDIDATE_ID_PATTERN = re.compile(
    r"candidate-v1-(?P<source>[0-9a-f]{16})-(?P<frame>[0-9]{9,})-"
    r"(?P<identity>[0-9a-f]{64})-(?P<occurrence>[0-9]{4,})"
)


class GlobalBallTrajectoryError(RuntimeError):
    """Raised when evidence cannot safely produce a global trajectory."""


@dataclass(frozen=True, slots=True)
class TrajectoryConfig:
    max_interpolation_gap: int = 6
    max_transition_gap: int = 12
    candidate_cap_per_frame: int = 24
    beam_width: int = 64
    match_probability_weight: float = 2.0
    detector_confidence_weight: float = 0.35
    pitch_prior_weight: float = 0.35
    player_foot_weight: float = 0.25
    detection_reward: float = 3.0
    speed_weight: float = 0.8
    acceleration_weight: float = 4.0
    direction_weight: float = 2.0
    gap_weight: float = 0.1
    restart_penalty: float = 2.0
    adjacent_restart_penalty: float = 10.0
    minimum_match_probability: float = 0.01
    interpolation_confidence_decay: float = 0.86


@dataclass(frozen=True, slots=True)
class _Snapshot:
    label: str
    path: Path
    sha256: str
    size: int
    stat_token: tuple[int, int, int, int, int]
    copy_path: Path | None = None


@dataclass(slots=True)
class _SourceLease:
    snapshot: _Snapshot
    handle: BinaryIO
    probe_path: Path


@dataclass(frozen=True, slots=True)
class _CandidateNode:
    candidate_id: str
    frame_index: int
    x: float
    y: float
    detector_confidence: float
    match_probability: float
    node_cost: float
    node_costs: dict[str, float]


@dataclass(frozen=True, slots=True)
class _FrontierState:
    state_id: int
    frame_index: int
    candidate_id: str
    x: float
    y: float
    velocity_x: float | None
    velocity_y: float | None
    total_cost: float


@dataclass(frozen=True, slots=True)
class _StateProposal:
    node: _CandidateNode
    previous: _FrontierState | None
    total_cost: float
    velocity_x: float | None
    velocity_y: float | None
    edge_costs: dict[str, float]
    restart: bool


class _BoundedJsonBuilder:
    """Build one JSON value while rejecting ambiguous or unbounded records."""

    def __init__(self, context: str) -> None:
        self._context = context
        self._builder = ijson.ObjectBuilder()
        self._containers: list[dict[str, Any]] = []
        self._token_count = 0

    @property
    def value(self) -> Any:
        return self._builder.value

    def event(self, event: str, value: Any) -> None:
        self._token_count += 1
        if self._token_count > _JSON_MAX_VALUE_TOKENS:
            raise GlobalBallTrajectoryError(f"JSON value exceeds token bound: {self._context}")
        if event in {"map_key", "string"}:
            if not isinstance(value, str) or len(value) > _JSON_MAX_STRING_CHARS:
                raise GlobalBallTrajectoryError(f"JSON string exceeds length bound: {self._context}")

        if event == "map_key":
            if not self._containers or self._containers[-1]["kind"] != "map":
                raise GlobalBallTrajectoryError(f"invalid JSON map structure: {self._context}")
            container = self._containers[-1]
            if container["awaiting_value"]:
                raise GlobalBallTrajectoryError(f"invalid JSON map structure: {self._context}")
            if value in container["keys"]:
                raise GlobalBallTrajectoryError(f"duplicate JSON key {value!r}: {self._context}")
            container["keys"].add(value)
            if len(container["keys"]) > _JSON_MAX_CONTAINER_ITEMS:
                raise GlobalBallTrajectoryError(f"JSON object exceeds item bound: {self._context}")
            container["awaiting_value"] = True
        elif event in {"start_map", "start_array"}:
            self._register_value()
            if len(self._containers) >= _JSON_MAX_DEPTH:
                raise GlobalBallTrajectoryError(f"JSON value exceeds depth bound: {self._context}")
            if event == "start_map":
                self._containers.append(
                    {"kind": "map", "keys": set(), "awaiting_value": False, "items": 0}
                )
            else:
                self._containers.append(
                    {"kind": "array", "keys": set(), "awaiting_value": False, "items": 0}
                )
        elif event in {"end_map", "end_array"}:
            expected_kind = "map" if event == "end_map" else "array"
            if not self._containers or self._containers[-1]["kind"] != expected_kind:
                raise GlobalBallTrajectoryError(f"invalid JSON container structure: {self._context}")
            if expected_kind == "map" and self._containers[-1]["awaiting_value"]:
                raise GlobalBallTrajectoryError(f"invalid JSON map structure: {self._context}")
            self._containers.pop()
        elif event in {"null", "boolean", "integer", "double", "number", "string"}:
            self._register_value()
        else:
            raise GlobalBallTrajectoryError(f"unsupported JSON event {event!r}: {self._context}")
        self._builder.event(event, value)

    def _register_value(self) -> None:
        if not self._containers:
            return
        container = self._containers[-1]
        if container["kind"] == "map":
            if not container["awaiting_value"]:
                raise GlobalBallTrajectoryError(f"invalid JSON map structure: {self._context}")
            container["awaiting_value"] = False
            return
        container["items"] += 1
        if container["items"] > _JSON_MAX_CONTAINER_ITEMS:
            raise GlobalBallTrajectoryError(f"JSON array exceeds item bound: {self._context}")


def solve_global_ball_trajectory(
    source_video_path: Path,
    source_contract_path: Path,
    predictions_path: Path,
    output_dir: Path,
    *,
    pitch_report_path: Path | None = None,
    player_tracks_path: Path | None = None,
    config: TrajectoryConfig | None = None,
) -> dict[str, Any]:
    """Solve one evidence-bound offline trajectory and atomically publish it."""

    validated_config = _validated_config(config or TrajectoryConfig())
    source_video_path = Path(source_video_path).resolve()
    source_contract_path = Path(source_contract_path).resolve()
    predictions_path = Path(predictions_path).resolve()
    output_dir = Path(output_dir).resolve()
    pitch_report_path = None if pitch_report_path is None else Path(pitch_report_path).resolve()
    player_tracks_path = None if player_tracks_path is None else Path(player_tracks_path).resolve()

    input_paths = [source_video_path, source_contract_path, predictions_path]
    input_paths.extend(path for path in (pitch_report_path, player_tracks_path) if path is not None)
    _validate_output_topology(output_dir, input_paths)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    output_lock: BinaryIO | None = None
    temp_root: Path | None = None
    connection: sqlite3.Connection | None = None
    source_lease: _SourceLease | None = None
    snapshots: list[_Snapshot] = []
    try:
        output_lock = _acquire_output_lock(output_dir)
        if output_dir.exists():
            raise GlobalBallTrajectoryError(
                "trajectory output directory already exists; publish each run to a new immutable generation"
            )
        temp_root = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.trajectory-", dir=output_dir.parent))
        staging_dir = temp_root / "generation"
        staging_dir.mkdir()
        database_path = temp_root / "trajectory.sqlite3"
        source_lease = _acquire_source_lease(source_video_path)
        source_snapshot = source_lease.snapshot
        contract_snapshot = _capture_snapshot(
            source_contract_path,
            "source tracking contract",
            copy_path=temp_root / "tracking_contract.snapshot.json",
        )
        predictions_snapshot = _capture_snapshot(
            predictions_path,
            "candidate predictions",
            copy_path=temp_root / "candidate_predictions.snapshot.json",
        )
        snapshots.extend((source_snapshot, contract_snapshot, predictions_snapshot))
        pitch_snapshot = _optional_snapshot(pitch_report_path, "pitch report", temp_root / "pitch.snapshot.json")
        player_snapshot = _optional_snapshot(
            player_tracks_path,
            "player tracks",
            temp_root / "player_tracks.snapshot.json",
        )
        if pitch_snapshot is not None:
            snapshots.append(pitch_snapshot)
        if player_snapshot is not None:
            snapshots.append(player_snapshot)

        metadata = _probe_video_metadata(source_lease.probe_path)
        connection = _open_database(database_path)
        _ingest_contract_once(
            connection,
            _snapshot_copy(contract_snapshot),
            source_snapshot.sha256,
            metadata,
        )
        _validate_frame_scope(connection, metadata)
        prediction_metadata = _ingest_predictions_once(
            connection,
            _snapshot_copy(predictions_snapshot),
            contract_snapshot.sha256,
        )
        prior_report = _ingest_optional_priors(
            connection,
            source_snapshot,
            metadata,
            pitch_snapshot,
            player_snapshot,
        )
        _prepare_candidate_costs(connection, validated_config, prior_report)
        work = _solve_candidate_graph(connection, validated_config, metadata)
        summary = _write_outputs(
            connection,
            staging_dir,
            validated_config,
            metadata,
            work,
        )
        report = _build_report(
            staging_dir,
            snapshots,
            metadata,
            prediction_metadata,
            validated_config,
            prior_report,
            work,
            summary,
        )
        _fsync_directory(staging_dir)
        for snapshot in snapshots:
            _verify_snapshot_stat(snapshot)
        if connection is not None:
            connection.close()
            connection = None
        _publish_generation(staging_dir, output_dir)
        try:
            for snapshot in snapshots:
                _verify_snapshot_stat(snapshot)
            _verify_source_lease(source_lease)
            _write_json_commit(output_dir / REPORT_NAME, report)
        except BaseException:
            _discard_published_generation(output_dir)
            raise
        return report
    except GlobalBallTrajectoryError:
        raise
    except KeyboardInterrupt:
        raise
    except BaseException as exc:
        raise GlobalBallTrajectoryError(str(exc) or exc.__class__.__name__) from exc
    finally:
        if connection is not None:
            try:
                connection.close()
            except BaseException:
                pass
        if source_lease is not None:
            try:
                _release_source_lease(source_lease)
            except BaseException:
                pass
        if output_lock is not None:
            try:
                _release_output_lock(output_lock)
            except BaseException:
                pass
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)


def _validate_output_topology(output_dir: Path, input_paths: list[Path]) -> None:
    for input_path in input_paths:
        if input_path == output_dir or output_dir in input_path.parents:
            raise GlobalBallTrajectoryError("trajectory output directory cannot contain an input artifact")


def _validated_config(config: TrajectoryConfig) -> TrajectoryConfig:
    integer_fields = (
        "max_interpolation_gap",
        "max_transition_gap",
        "candidate_cap_per_frame",
        "beam_width",
    )
    for name in integer_fields:
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < (0 if "gap" in name else 1):
            raise GlobalBallTrajectoryError(f"{name} must be a valid non-negative bound")
    if config.candidate_cap_per_frame < 1 or config.beam_width < 1:
        raise GlobalBallTrajectoryError("candidate and beam bounds must be positive")
    for name, value in asdict(config).items():
        if name in integer_fields:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise GlobalBallTrajectoryError(f"{name} must be finite")
        if float(value) < 0.0:
            raise GlobalBallTrajectoryError(f"{name} must be non-negative")
    if not 0.0 < config.interpolation_confidence_decay <= 1.0:
        raise GlobalBallTrajectoryError("interpolation_confidence_decay must be in (0, 1]")
    if not 0.0 <= config.minimum_match_probability <= 1.0:
        raise GlobalBallTrajectoryError("minimum_match_probability must be in [0, 1]")
    return config


def _stat_token(stat_result: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
        int(getattr(stat_result, "st_ctime_ns", 0)),
        int(getattr(stat_result, "st_ino", 0)),
        int(getattr(stat_result, "st_dev", 0)),
    )


def _acquire_source_lease(path: Path) -> _SourceLease:
    if not path.is_file():
        raise GlobalBallTrajectoryError(f"source video does not exist: {path}")
    handle: BinaryIO | None = None
    try:
        handle = _open_source_lease_handle(path)
        probe_path = _leased_probe_path(path, handle)
        if probe_path is None:
            raise GlobalBallTrajectoryError(
                "the platform cannot expose the leased source file to the video metadata probe"
            )
        before = os.fstat(handle.fileno())
        digest = _hash_source_handle(handle)
        after = os.fstat(handle.fileno())
        current = path.stat()
        if _stat_token(before) != _stat_token(after) or _stat_token(after) != _stat_token(current):
            raise GlobalBallTrajectoryError("source video changed while its stable lease was being captured")
        snapshot = _Snapshot(
            label="source video",
            path=path,
            sha256=digest,
            size=int(after.st_size),
            stat_token=_stat_token(after),
            copy_path=None,
        )
        return _SourceLease(snapshot=snapshot, handle=handle, probe_path=probe_path)
    except GlobalBallTrajectoryError:
        if handle is not None:
            _try_close_source_lease_handle(handle)
        raise
    except (OSError, ValueError) as exc:
        if handle is not None:
            _try_close_source_lease_handle(handle)
        raise GlobalBallTrajectoryError(f"cannot acquire stable source video lease: {exc}") from exc
    except BaseException:
        if handle is not None:
            _try_close_source_lease_handle(handle)
        raise


def _open_source_lease_handle(path: Path) -> BinaryIO:
    if os.name != "nt":
        handle = path.open("rb")
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BaseException:
            handle.close()
            raise
        return handle

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    raw_handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001,  # FILE_SHARE_READ: deny write and delete/rename for the lease lifetime
        None,
        3,  # OPEN_EXISTING
        0x08000080,  # FILE_FLAG_SEQUENTIAL_SCAN | FILE_ATTRIBUTE_NORMAL
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if raw_handle is None or int(raw_handle) == invalid_handle:
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error), str(path))
    descriptor: int | None = None
    try:
        descriptor = msvcrt.open_osfhandle(
            int(raw_handle),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        return os.fdopen(descriptor, "rb", closefd=True)
    except BaseException:
        if descriptor is None:
            close_handle(raw_handle)
        else:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _leased_probe_path(path: Path, handle: BinaryIO) -> Path | None:
    if os.name == "nt":
        return path
    for root in (Path("/proc/self/fd"), Path("/dev/fd")):
        candidate = root / str(handle.fileno())
        try:
            candidate_stat = candidate.stat()
            handle_stat = os.fstat(handle.fileno())
        except OSError:
            continue
        if (
            int(getattr(candidate_stat, "st_dev", -1)) == int(getattr(handle_stat, "st_dev", -2))
            and int(getattr(candidate_stat, "st_ino", -1)) == int(getattr(handle_stat, "st_ino", -2))
        ):
            return candidate
    return None


def _hash_source_handle(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    handle.seek(0)
    while True:
        chunk = handle.read(_COPY_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
    handle.seek(0)
    return digest.hexdigest()


def _verify_source_lease(lease: _SourceLease) -> None:
    before = os.fstat(lease.handle.fileno())
    digest = _hash_source_handle(lease.handle)
    after = os.fstat(lease.handle.fileno())
    try:
        current = lease.snapshot.path.stat()
    except OSError as exc:
        raise GlobalBallTrajectoryError("source video changed during trajectory solving") from exc
    expected = lease.snapshot.stat_token
    if (
        _stat_token(before) != expected
        or _stat_token(after) != expected
        or _stat_token(current) != expected
        or digest != lease.snapshot.sha256
    ):
        raise GlobalBallTrajectoryError("source video changed during trajectory solving")


def _release_source_lease(lease: _SourceLease) -> None:
    _close_source_lease_handle(lease.handle)


def _try_close_source_lease_handle(handle: BinaryIO) -> None:
    try:
        _close_source_lease_handle(handle)
    except BaseException:
        pass


def _close_source_lease_handle(handle: BinaryIO) -> None:
    if os.name != "nt":
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except BaseException:
            pass
    handle.close()


def _capture_snapshot(path: Path, label: str, *, copy_path: Path | None = None) -> _Snapshot:
    if not path.is_file():
        raise GlobalBallTrajectoryError(f"{label} does not exist: {path}")
    before = path.stat()
    digest = hashlib.sha256()
    copied: BinaryIO | None = None
    try:
        if copy_path is not None:
            copied = copy_path.open("xb")
        with path.open("rb") as source:
            while True:
                chunk = source.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                if copied is not None:
                    copied.write(chunk)
        if copied is not None:
            copied.flush()
            os.fsync(copied.fileno())
    except OSError as exc:
        raise GlobalBallTrajectoryError(f"cannot snapshot {label}: {exc}") from exc
    finally:
        if copied is not None:
            copied.close()
    after = path.stat()
    if _stat_token(before) != _stat_token(after):
        raise GlobalBallTrajectoryError(f"{label} changed while it was being captured")
    return _Snapshot(
        label=label,
        path=path,
        sha256=digest.hexdigest(),
        size=int(after.st_size),
        stat_token=_stat_token(after),
        copy_path=copy_path,
    )


def _optional_snapshot(path: Path | None, label: str, copy_path: Path) -> _Snapshot | None:
    if path is None:
        return None
    return _capture_snapshot(path, label, copy_path=copy_path)


def _verify_snapshot_stat(snapshot: _Snapshot) -> None:
    try:
        current = snapshot.path.stat()
    except OSError as exc:
        raise GlobalBallTrajectoryError(f"{snapshot.label} changed during trajectory solving") from exc
    if not snapshot.path.is_file() or _stat_token(current) != snapshot.stat_token:
        raise GlobalBallTrajectoryError(f"{snapshot.label} changed during trajectory solving")


def _snapshot_copy(snapshot: _Snapshot) -> Path:
    if snapshot.copy_path is None or not snapshot.copy_path.is_file():
        raise GlobalBallTrajectoryError(f"{snapshot.label} snapshot copy is unavailable")
    return snapshot.copy_path


def _probe_video_metadata(path: Path) -> dict[str, Any]:
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise GlobalBallTrajectoryError(f"cannot open source video: {path}")
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        width = int(round(float(capture.get(cv2.CAP_PROP_FRAME_WIDTH))))
        height = int(round(float(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))))
        frame_count = int(round(float(capture.get(cv2.CAP_PROP_FRAME_COUNT))))
    finally:
        capture.release()
    if not math.isfinite(fps) or fps <= 0.0 or width <= 0 or height <= 0 or frame_count <= 0:
        raise GlobalBallTrajectoryError("source video metadata is incomplete")
    return {"fps": fps, "width": width, "height": height, "frame_count": frame_count}


def _open_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(
        """
        CREATE TABLE frames (
            frame_index INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            x REAL,
            y REAL,
            confidence REAL,
            source TEXT,
            reason TEXT,
            legacy_status TEXT
        );
        CREATE TABLE candidates (
            candidate_id TEXT PRIMARY KEY,
            frame_index INTEGER NOT NULL REFERENCES frames(frame_index),
            x REAL NOT NULL,
            y REAL NOT NULL,
            x1 REAL NOT NULL,
            y1 REAL NOT NULL,
            x2 REAL NOT NULL,
            y2 REAL NOT NULL,
            detector_confidence REAL NOT NULL,
            detector_source TEXT NOT NULL,
            match_probability REAL,
            predicted_label TEXT,
            prediction_confidence REAL,
            node_cost REAL,
            node_costs_json TEXT,
            hard_reject_reason TEXT,
            budget_pruned INTEGER NOT NULL DEFAULT 0,
            beam_pruned INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX candidates_frame_idx ON candidates(frame_index, candidate_id);
        CREATE TABLE candidate_identity_occurrences (
            identity_sha256 TEXT NOT NULL,
            occurrence INTEGER NOT NULL,
            PRIMARY KEY (identity_sha256, occurrence)
        );
        CREATE TABLE classifications (
            candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
            label TEXT NOT NULL,
            label_origin TEXT NOT NULL,
            confidence REAL,
            UNIQUE (candidate_id, label_origin)
        );
        CREATE TABLE raw_decisions (
            candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
            decision TEXT NOT NULL,
            confidence REAL NOT NULL,
            reason TEXT,
            UNIQUE (candidate_id)
        );
        CREATE TABLE predictions (
            candidate_id TEXT PRIMARY KEY REFERENCES candidates(candidate_id),
            candidate_fingerprint TEXT NOT NULL,
            model_version TEXT NOT NULL,
            probabilities_json TEXT NOT NULL
        );
        CREATE TABLE player_feet (
            frame_index INTEGER NOT NULL,
            x REAL NOT NULL,
            y REAL NOT NULL
        );
        CREATE INDEX player_feet_frame_idx ON player_feet(frame_index);
        CREATE TABLE dp_states (
            state_id INTEGER PRIMARY KEY AUTOINCREMENT,
            frame_index INTEGER NOT NULL,
            candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
            previous_state_id INTEGER,
            total_cost REAL NOT NULL,
            velocity_x REAL,
            velocity_y REAL,
            edge_costs_json TEXT NOT NULL,
            restart INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE selected (
            frame_index INTEGER PRIMARY KEY,
            candidate_id TEXT UNIQUE NOT NULL REFERENCES candidates(candidate_id),
            state_id INTEGER NOT NULL REFERENCES dp_states(state_id)
        );
        CREATE INDEX dp_states_candidate_cost_idx
            ON dp_states(candidate_id, total_cost, state_id);
        """
    )
    return connection


def _stream_items(path: Path, prefix: str) -> Iterator[Any]:
    builder: _BoundedJsonBuilder | None = None
    try:
        with path.open("rb") as handle:
            for current_prefix, event, value in ijson.parse(handle, use_float=True):
                if builder is not None:
                    builder.event(event, value)
                    if current_prefix == prefix and event in {"end_map", "end_array"}:
                        yield builder.value
                        builder = None
                    continue
                if current_prefix != prefix:
                    continue
                if event in {"start_map", "start_array"}:
                    builder = _BoundedJsonBuilder(f"{path.name}:{prefix}")
                    builder.event(event, value)
                elif event not in {"end_map", "end_array", "map_key"}:
                    _validate_json_scalar(event, value, f"{path.name}:{prefix}")
                    yield value
    except (OSError, UnicodeDecodeError, ValueError, ijson.JSONError) as exc:
        if isinstance(exc, GlobalBallTrajectoryError):
            raise
        raise GlobalBallTrajectoryError(f"invalid JSON evidence in {path.name}: {exc}") from exc


def _parse_top_level_json(
    path: Path,
    *,
    streamed_arrays: dict[str, Callable[[Any], None]],
    captured_values: set[str],
) -> tuple[dict[str, Any], set[str]]:
    captured: dict[str, Any] = {}
    arrays_seen: set[str] = set()
    top_keys_seen: set[str] = set()
    allowed_top_keys = set(streamed_arrays) | captured_values
    builder: _BoundedJsonBuilder | None = None
    builder_prefix: str | None = None
    builder_end_event: str | None = None
    builder_callback: Callable[[Any], None] | None = None
    root_started = False
    root_ended = False
    try:
        with path.open("rb") as handle:
            for prefix, event, value in ijson.parse(handle, use_float=True):
                if not root_started:
                    if prefix != "" or event != "start_map":
                        raise GlobalBallTrajectoryError("JSON evidence root must be an object")
                    root_started = True
                    continue
                if prefix == "" and event == "end_map":
                    root_ended = True
                    continue
                if builder is not None:
                    builder.event(event, value)
                    if prefix == builder_prefix and event == builder_end_event:
                        built_value = builder.value
                        assert builder_callback is not None
                        builder_callback(built_value)
                        builder = None
                        builder_prefix = None
                        builder_end_event = None
                        builder_callback = None
                    continue

                if prefix == "" and event == "map_key":
                    key = str(value)
                    if key in top_keys_seen:
                        raise GlobalBallTrajectoryError(f"duplicate top-level JSON key: {key}")
                    if key not in allowed_top_keys:
                        raise GlobalBallTrajectoryError(f"unexpected top-level JSON key: {key}")
                    top_keys_seen.add(key)
                    continue

                if prefix in streamed_arrays:
                    if event == "start_array":
                        arrays_seen.add(prefix)
                    elif event != "end_array":
                        raise GlobalBallTrajectoryError(f"{prefix} must be an array")
                    continue

                array_name = prefix.split(".", 1)[0]
                item_prefix = f"{array_name}.item"
                if array_name in streamed_arrays and prefix == item_prefix:
                    callback = streamed_arrays[array_name]
                    if event == "start_map":
                        builder = _BoundedJsonBuilder(f"{path.name}:{prefix}")
                        builder_prefix = prefix
                        builder_end_event = "end_map"
                        builder_callback = callback
                        builder.event(event, value)
                    elif event == "start_array":
                        builder = _BoundedJsonBuilder(f"{path.name}:{prefix}")
                        builder_prefix = prefix
                        builder_end_event = "end_array"
                        builder_callback = callback
                        builder.event(event, value)
                    elif event not in {"end_array", "end_map"}:
                        _validate_json_scalar(event, value, f"{path.name}:{prefix}")
                        callback(value)
                    continue

                if prefix in captured_values:
                    if prefix in captured:
                        raise GlobalBallTrajectoryError(f"duplicate top-level JSON value: {prefix}")
                    if event == "start_map":
                        builder = _BoundedJsonBuilder(f"{path.name}:{prefix}")
                        builder_prefix = prefix
                        builder_end_event = "end_map"
                        builder_callback = partial(captured.__setitem__, prefix)
                        builder.event(event, value)
                    elif event == "start_array":
                        builder = _BoundedJsonBuilder(f"{path.name}:{prefix}")
                        builder_prefix = prefix
                        builder_end_event = "end_array"
                        builder_callback = partial(captured.__setitem__, prefix)
                        builder.event(event, value)
                    elif event not in {"map_key", "end_map", "end_array"}:
                        _validate_json_scalar(event, value, f"{path.name}:{prefix}")
                        captured[prefix] = value
    except (OSError, UnicodeDecodeError, ValueError, ijson.JSONError) as exc:
        if isinstance(exc, GlobalBallTrajectoryError):
            raise
        raise GlobalBallTrajectoryError(f"invalid JSON evidence in {path.name}: {exc}") from exc
    if not root_started or not root_ended or builder is not None:
        raise GlobalBallTrajectoryError(f"invalid JSON evidence in {path.name}: incomplete document")
    return captured, arrays_seen


def _validate_json_scalar(event: str, value: Any, context: str) -> None:
    if event not in {"null", "boolean", "integer", "double", "number", "string"}:
        raise GlobalBallTrajectoryError(f"invalid JSON scalar event {event!r}: {context}")
    if event == "string" and (not isinstance(value, str) or len(value) > _JSON_MAX_STRING_CHARS):
        raise GlobalBallTrajectoryError(f"JSON string exceeds length bound: {context}")


def _finite_float(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise GlobalBallTrajectoryError(f"{name} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GlobalBallTrajectoryError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise GlobalBallTrajectoryError(f"{name} must be finite")
    return result


def _probability(value: Any, name: str) -> float:
    result = _finite_float(value, name)
    if not 0.0 <= result <= 1.0:
        raise GlobalBallTrajectoryError(f"{name} must be in [0, 1]")
    return result


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GlobalBallTrajectoryError(f"{name} must be a non-negative integer")
    return value


def _positive_int(value: Any, name: str) -> int:
    result = _nonnegative_int(value, name)
    if result == 0:
        raise GlobalBallTrajectoryError(f"{name} must be a positive integer")
    return result


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GlobalBallTrajectoryError(f"{name} must be a non-empty string")
    return value


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _ingest_contract_once(
    connection: sqlite3.Connection,
    path: Path,
    source_sha256: str,
    metadata: dict[str, Any],
) -> None:
    counts = {"frames": 0, "candidates": 0, "classifications": 0, "decisions": 0}
    source_errors: list[str] = []
    connection.execute("PRAGMA defer_foreign_keys = ON")

    handlers: dict[str, Callable[[Any], None]] = {
        "frames": lambda raw: _insert_contract_frame(connection, raw, counts),
        "candidates": lambda raw: _insert_contract_candidate(
            connection, raw, source_sha256, metadata, counts
        ),
        "classifications": lambda raw: _insert_contract_classification(connection, raw, counts),
        "decisions": lambda raw: _insert_contract_decision(connection, raw, counts),
        "validation_errors": lambda raw: source_errors.append(
            _required_text(raw, "tracking contract validation error")
        ),
    }
    captured, arrays_seen = _parse_top_level_json(
        path,
        streamed_arrays=handlers,
        captured_values={"schema_version", "source", "summary", "generated_at"},
    )
    if arrays_seen != set(handlers):
        missing = sorted(set(handlers) - arrays_seen)
        raise GlobalBallTrajectoryError(f"tracking contract arrays are missing: {missing}")
    if captured.get("schema_version") != TRACKING_CONTRACT_SCHEMA_VERSION:
        raise GlobalBallTrajectoryError("unsupported tracking contract schema_version")
    if not isinstance(captured.get("generated_at"), str) or not captured["generated_at"].strip():
        raise GlobalBallTrajectoryError("tracking contract generated_at must be a non-empty string")
    source = captured.get("source")
    if not isinstance(source, dict) or source.get("video_sha256") != source_sha256:
        raise GlobalBallTrajectoryError("tracking contract source video sha256 does not match")
    if not set(source).issubset(SOURCE_LINEAGE_FIELDS):
        raise GlobalBallTrajectoryError("tracking contract source contains unsupported fields")
    missing_source_metadata = {"fps", "width", "height", "frame_count"} - set(source)
    if missing_source_metadata:
        raise GlobalBallTrajectoryError(
            f"tracking contract source metadata is missing: {sorted(missing_source_metadata)}"
        )
    for name in ("width", "height", "frame_count"):
        declared_value = _positive_int(source[name], f"tracking contract source {name}")
        if declared_value != metadata[name]:
            raise GlobalBallTrajectoryError(f"tracking contract source {name} does not match")
    if "fps" in source and not math.isclose(
        _finite_float(source["fps"], "tracking contract source fps"),
        float(metadata["fps"]),
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        raise GlobalBallTrajectoryError("tracking contract source fps does not match")
    if source_errors:
        raise GlobalBallTrajectoryError("source tracking contract is invalid")
    _validate_contract_summary(captured.get("summary"), counts)

    violations = list(connection.execute("PRAGMA foreign_key_check"))
    if violations:
        table = str(violations[0][0])
        if table == "candidates":
            raise GlobalBallTrajectoryError("candidate references an absent frame")
        raise GlobalBallTrajectoryError(f"{table} references absent contract evidence")
    discontinuous = connection.execute(
        """
        SELECT identity_sha256
        FROM candidate_identity_occurrences
        GROUP BY identity_sha256
        HAVING MIN(occurrence) <> 0 OR MAX(occurrence) <> COUNT(*) - 1
        LIMIT 1
        """
    ).fetchone()
    if discontinuous is not None:
        raise GlobalBallTrajectoryError("candidate-v1 duplicate occurrences are not contiguous")
    connection.commit()


def _insert_contract_frame(
    connection: sqlite3.Connection,
    raw: Any,
    counts: dict[str, int],
) -> None:
    if not isinstance(raw, dict):
        raise GlobalBallTrajectoryError("tracking contract frame must be an object")
    frame_index = _nonnegative_int(raw.get("frame_index"), "frame.frame_index")
    status = raw.get("status")
    legacy_status = raw.get("legacy_status")
    if status in LEGACY_STATUS_MAP:
        legacy_status = status
        status = LEGACY_STATUS_MAP[status]
    if status not in FRAME_STATUSES:
        raise GlobalBallTrajectoryError("tracking contract frame status is invalid")
    x = None if raw.get("x") is None else _finite_float(raw.get("x"), "frame.x")
    y = None if raw.get("y") is None else _finite_float(raw.get("y"), "frame.y")
    if (x is None) != (y is None):
        raise GlobalBallTrajectoryError("tracking frame coordinates must be paired")
    if status in {"detected", "interpolated"} and x is None:
        raise GlobalBallTrajectoryError("tracking frame coordinates are required for its status")
    confidence = None
    if raw.get("confidence") is not None:
        confidence = _probability(raw.get("confidence"), "frame.confidence")
    try:
        connection.execute(
            "INSERT INTO frames VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                frame_index,
                status,
                x,
                y,
                confidence,
                _optional_text(raw.get("source"), "frame.source"),
                _optional_text(raw.get("reason"), "frame.reason"),
                legacy_status,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise GlobalBallTrajectoryError("tracking contract contains duplicate frames") from exc
    counts["frames"] += 1


def _insert_contract_candidate(
    connection: sqlite3.Connection,
    raw: Any,
    source_sha256: str,
    metadata: dict[str, Any],
    counts: dict[str, int],
) -> None:
    if not isinstance(raw, dict):
        raise GlobalBallTrajectoryError("tracking candidate must be an object")
    candidate_id = _required_text(raw.get("candidate_id"), "candidate.candidate_id")
    frame_index = _nonnegative_int(raw.get("frame_index"), "candidate.frame_index")
    bbox = raw.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise GlobalBallTrajectoryError("candidate.bbox must contain four coordinates")
    x1, y1, x2, y2 = (_finite_float(value, "candidate.bbox") for value in bbox)
    if x2 <= x1 or y2 <= y1:
        raise GlobalBallTrajectoryError("candidate.bbox is invalid")
    if x1 < 0.0 or y1 < 0.0 or x2 > float(metadata["width"]) or y2 > float(metadata["height"]):
        raise GlobalBallTrajectoryError("candidate.bbox lies outside the source frame")
    center_x = x1 + (x2 - x1) / 2.0
    center_y = y1 + (y2 - y1) / 2.0
    if not math.isfinite(center_x) or not math.isfinite(center_y):
        raise GlobalBallTrajectoryError("candidate center must be finite")
    confidence = _probability(raw.get("confidence"), "candidate.confidence")
    detector_source = _required_text(raw.get("source"), "candidate.source")
    identity_sha256, occurrence = _validate_candidate_id_record(
        candidate_id,
        frame_index=frame_index,
        bbox=[x1, y1, x2, y2],
        confidence=confidence,
        source=detector_source,
        source_sha256=source_sha256,
    )
    try:
        connection.execute(
            """
            INSERT INTO candidates (
                candidate_id, frame_index, x, y, x1, y1, x2, y2,
                detector_confidence, detector_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                frame_index,
                center_x,
                center_y,
                x1,
                y1,
                x2,
                y2,
                confidence,
                detector_source,
            ),
        )
        connection.execute(
            "INSERT INTO candidate_identity_occurrences VALUES (?, ?)",
            (identity_sha256, occurrence),
        )
    except sqlite3.IntegrityError as exc:
        raise GlobalBallTrajectoryError("duplicate or invalid candidate identity") from exc
    counts["candidates"] += 1


def _insert_contract_classification(
    connection: sqlite3.Connection,
    raw: Any,
    counts: dict[str, int],
) -> None:
    if not isinstance(raw, dict):
        raise GlobalBallTrajectoryError("classification must be an object")
    candidate_id = _required_text(raw.get("candidate_id"), "classification.candidate_id")
    label = raw.get("label")
    origin = raw.get("label_origin")
    if label not in CLASSIFICATION_LABELS or origin not in LABEL_ORIGINS:
        raise GlobalBallTrajectoryError("classification label or origin is invalid")
    confidence = None
    if raw.get("confidence") is not None:
        confidence = _probability(raw.get("confidence"), "classification.confidence")
    try:
        connection.execute(
            "INSERT INTO classifications VALUES (?, ?, ?, ?)",
            (candidate_id, label, origin, confidence),
        )
    except sqlite3.IntegrityError as exc:
        raise GlobalBallTrajectoryError("duplicate or conflicting classification") from exc
    counts["classifications"] += 1


def _insert_contract_decision(
    connection: sqlite3.Connection,
    raw: Any,
    counts: dict[str, int],
) -> None:
    if not isinstance(raw, dict):
        raise GlobalBallTrajectoryError("selective decision must be an object")
    candidate_id = _required_text(raw.get("candidate_id"), "decision.candidate_id")
    decision = raw.get("decision")
    if decision not in {"accept", "reject", "abstain"}:
        raise GlobalBallTrajectoryError("selective decision is invalid")
    confidence = _probability(raw.get("confidence"), "decision.confidence")
    try:
        connection.execute(
            "INSERT INTO raw_decisions VALUES (?, ?, ?, ?)",
            (candidate_id, decision, confidence, _optional_text(raw.get("reason"), "decision.reason")),
        )
    except sqlite3.IntegrityError as exc:
        raise GlobalBallTrajectoryError("duplicate or conflicting unvalidated selective decision") from exc
    counts["decisions"] += 1


def _validate_contract_summary(value: Any, counts: dict[str, int]) -> None:
    if not isinstance(value, dict):
        raise GlobalBallTrajectoryError("tracking contract summary must be an object")
    expected = {
        "frame_count": counts["frames"],
        "candidate_count": counts["candidates"],
        "classification_count": counts["classifications"],
        "decision_count": counts["decisions"],
        "validation_error_count": 0,
    }
    for name, count in expected.items():
        declared_count = _nonnegative_int(value.get(name), f"tracking contract summary {name}")
        if declared_count != count:
            raise GlobalBallTrajectoryError(f"tracking contract summary {name} does not match its records")
    expected_status = "ok" if sum(counts.values()) else "empty"
    if value.get("status") != expected_status:
        raise GlobalBallTrajectoryError("tracking contract summary status does not match its records")


def _validate_candidate_id_record(
    candidate_id: str,
    *,
    frame_index: int,
    bbox: list[float],
    confidence: float,
    source: str,
    source_sha256: str,
) -> tuple[str, int]:
    match = _CANDIDATE_ID_PATTERN.fullmatch(candidate_id)
    if match is None:
        raise GlobalBallTrajectoryError("candidate-v1 identity format is invalid")
    if match.group("source") != source_sha256[:16] or match.group("frame") != f"{frame_index:09d}":
        raise GlobalBallTrajectoryError("candidate-v1 identity does not match its source video or frame")
    persisted = {
        "frame_index": frame_index,
        "bbox": [_canonical_float(value) for value in bbox],
        "confidence": _canonical_float(confidence),
        "source": source.strip(),
    }
    identity = _canonical_json(persisted)
    expected_identity = hashlib.sha256(f"v1\0{source_sha256}\0{identity}".encode("utf-8")).hexdigest()
    if match.group("identity") != expected_identity:
        raise GlobalBallTrajectoryError("candidate-v1 identity does not match detector evidence")
    occurrence = int(match.group("occurrence"))
    if match.group("occurrence") != f"{occurrence:04d}":
        raise GlobalBallTrajectoryError("candidate-v1 duplicate occurrence is not canonically encoded")
    return expected_identity, occurrence


def _canonical_float(value: float) -> str:
    parsed = float(value)
    if parsed == 0.0:
        parsed = 0.0
    return parsed.hex()


def _validate_frame_scope(connection: sqlite3.Connection, metadata: dict[str, Any]) -> None:
    scope = connection.execute("SELECT COUNT(*), MIN(frame_index), MAX(frame_index) FROM frames").fetchone()
    if scope is None or scope[1] is None or scope[2] is None:
        raise GlobalBallTrajectoryError("tracking contract contains no frame scope")
    expected_count = int(metadata["frame_count"])
    if int(scope[0]) != expected_count or int(scope[1]) != 0 or int(scope[2]) != expected_count - 1:
        raise GlobalBallTrajectoryError("tracking contract frame scope must cover every source frame exactly once")


def _ingest_predictions_once(
    connection: sqlite3.Connection,
    path: Path,
    contract_sha256: str,
) -> dict[str, Any]:
    actual_count = 0

    def insert_prediction(raw: Any) -> None:
        nonlocal actual_count
        _insert_prediction(connection, raw)
        actual_count += 1

    captured, arrays_seen = _parse_top_level_json(
        path,
        streamed_arrays={"predictions": insert_prediction},
        captured_values={
            "schema_version",
            "artifact_type",
            "model_version",
            "dataset_version",
            "source_contract_sha256",
            "class_order",
            "temperature",
            "prediction_count",
        },
    )
    if arrays_seen != {"predictions"}:
        raise GlobalBallTrajectoryError("candidate predictions array is missing")
    if captured.get("schema_version") != "1.0":
        raise GlobalBallTrajectoryError("unsupported candidate predictions schema")
    if captured.get("artifact_type") != "candidate_predictions":
        raise GlobalBallTrajectoryError("invalid candidate predictions artifact_type")
    model_version = _sha256_text(captured.get("model_version"), "model_version")
    dataset_version = _sha256_text(captured.get("dataset_version"), "dataset_version")
    bound_contract = _sha256_text(captured.get("source_contract_sha256"), "source_contract_sha256")
    if bound_contract != contract_sha256:
        raise GlobalBallTrajectoryError("candidate predictions source contract sha256 does not match")
    if captured.get("class_order") != list(CLASSIFICATION_LABELS):
        raise GlobalBallTrajectoryError("candidate predictions class_order does not match the classifier contract")
    temperature = _finite_float(captured.get("temperature"), "temperature")
    if temperature <= 0.0:
        raise GlobalBallTrajectoryError("predictions temperature must be positive")
    declared_count = _nonnegative_int(captured.get("prediction_count"), "prediction_count")
    candidate_count = int(connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0])
    if actual_count != declared_count or actual_count != candidate_count:
        raise GlobalBallTrajectoryError("candidate prediction population does not exactly match the contract")
    mismatched_model = connection.execute(
        "SELECT candidate_id FROM predictions WHERE model_version <> ? LIMIT 1",
        (model_version,),
    ).fetchone()
    if mismatched_model is not None:
        raise GlobalBallTrajectoryError("candidate prediction model_version mismatch")
    connection.commit()
    return {
        "model_version": model_version,
        "dataset_version": dataset_version,
        "temperature": temperature,
        "prediction_count": actual_count,
    }


def _insert_prediction(connection: sqlite3.Connection, raw: Any) -> None:
    if not isinstance(raw, dict):
        raise GlobalBallTrajectoryError("prediction must be an object")
    candidate_id = _required_text(raw.get("candidate_id"), "prediction.candidate_id")
    row = connection.execute(
        """
        SELECT frame_index, x1, y1, x2, y2, detector_source, detector_confidence
        FROM candidates WHERE candidate_id = ?
        """,
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise GlobalBallTrajectoryError("prediction references a dangling candidate")
    fingerprint = _sha256_text(raw.get("candidate_fingerprint"), "candidate_fingerprint")
    if fingerprint != _candidate_fingerprint(candidate_id, row):
        raise GlobalBallTrajectoryError("candidate prediction fingerprint does not match contract evidence")
    row_model_version = _sha256_text(raw.get("model_version"), "prediction.model_version")
    probabilities = raw.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != set(CLASSIFICATION_LABELS):
        raise GlobalBallTrajectoryError("prediction probabilities must contain the exact class set")
    normalized = {
        label: _probability(probabilities[label], f"prediction.probabilities.{label}")
        for label in CLASSIFICATION_LABELS
    }
    if abs(sum(normalized.values()) - 1.0) > _PROBABILITY_TOLERANCE:
        raise GlobalBallTrajectoryError("prediction probabilities must sum to one")
    predicted_label = raw.get("predicted_label")
    expected_label = max(
        CLASSIFICATION_LABELS,
        key=lambda label: (normalized[label], -CLASSIFICATION_LABELS.index(label)),
    )
    if not isinstance(predicted_label, str) or predicted_label not in CLASSIFICATION_LABELS:
        raise GlobalBallTrajectoryError("prediction label is invalid")
    if predicted_label != expected_label:
        raise GlobalBallTrajectoryError("prediction label does not match probabilities")
    confidence = _probability(raw.get("confidence"), "prediction.confidence")
    if not math.isclose(
        confidence,
        normalized[predicted_label],
        rel_tol=0.0,
        abs_tol=_PROBABILITY_TOLERANCE,
    ):
        raise GlobalBallTrajectoryError("prediction confidence does not match probabilities")
    try:
        connection.execute(
            "INSERT INTO predictions VALUES (?, ?, ?, ?)",
            (candidate_id, fingerprint, row_model_version, _canonical_json(normalized)),
        )
    except sqlite3.IntegrityError as exc:
        raise GlobalBallTrajectoryError("duplicate candidate prediction") from exc
    connection.execute(
        """
        UPDATE candidates
        SET match_probability = ?, predicted_label = ?, prediction_confidence = ?
        WHERE candidate_id = ?
        """,
        (normalized["match_ball"], predicted_label, confidence, candidate_id),
    )


def _sha256_text(value: Any, name: str) -> str:
    value = _required_text(value, name)
    if len(value) != _SHA256_LENGTH or any(character not in "0123456789abcdef" for character in value):
        raise GlobalBallTrajectoryError(f"{name} must be a lowercase SHA-256")
    return value


def _candidate_fingerprint(candidate_id: str, row: sqlite3.Row | tuple[Any, ...]) -> str:
    identity = {
        "candidate_id": candidate_id,
        "frame_index": int(row[0]),
        "bbox": [float(row[1]), float(row[2]), float(row[3]), float(row[4])],
        "detector_source": row[5],
        "confidence": float(row[6]),
    }
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _ingest_optional_priors(
    connection: sqlite3.Connection,
    source_snapshot: _Snapshot,
    metadata: dict[str, Any],
    pitch_snapshot: _Snapshot | None,
    player_snapshot: _Snapshot | None,
) -> dict[str, Any]:
    pitch_report = _pitch_prior_report(pitch_snapshot, source_snapshot, metadata)
    player_report = _player_prior_report(connection, player_snapshot, source_snapshot, metadata)
    connection.commit()
    return {"pitch": pitch_report, "player_foot": player_report}


def _pitch_prior_report(
    snapshot: _Snapshot | None,
    source_snapshot: _Snapshot,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    if snapshot is None:
        return {"status": "neutral", "reason": "not_provided"}
    path = _snapshot_copy(snapshot)
    lineage = _optional_top_level_mapping(path, "lineage")
    if not _lineage_matches(lineage, source_snapshot, metadata):
        return {"status": "neutral", "reason": "source_lineage_missing_or_mismatched"}
    polygon = _optional_top_level_value(path, "pitch_polygon")
    if polygon is None:
        polygon = _optional_top_level_value(path, "field_polygon")
    parsed = _parse_polygon(polygon)
    if parsed is None:
        return {"status": "neutral", "reason": "pitch_polygon_unavailable"}
    return {
        "status": "loaded",
        "reason": "source_bound_polygon",
        "polygon": [[point[0], point[1]] for point in parsed],
    }


def _player_prior_report(
    connection: sqlite3.Connection,
    snapshot: _Snapshot | None,
    source_snapshot: _Snapshot,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    if snapshot is None:
        return {"status": "neutral", "reason": "not_provided", "sample_count": 0}
    path = _snapshot_copy(snapshot)
    lineage = _optional_top_level_mapping(path, "lineage")
    if not _lineage_matches(lineage, source_snapshot, metadata):
        reason = "source_lineage_missing" if not lineage else "source_lineage_mismatch"
        return {"status": "neutral", "reason": reason, "sample_count": 0}

    sample_count = 0
    for sample in _stream_items(path, "tracks.item.samples.item"):
        if not isinstance(sample, dict):
            raise GlobalBallTrajectoryError("player track sample must be an object")
        frame = _nonnegative_int(sample.get("frame"), "player sample frame")
        if frame >= int(metadata["frame_count"]):
            raise GlobalBallTrajectoryError("player track sample exceeds the source frame scope")
        point = sample.get("foot_point")
        if not isinstance(point, dict):
            raise GlobalBallTrajectoryError("player track foot_point is missing")
        x = _finite_float(point.get("x"), "player foot x")
        y = _finite_float(point.get("y"), "player foot y")
        if not 0.0 <= x <= float(metadata["width"]) or not 0.0 <= y <= float(metadata["height"]):
            raise GlobalBallTrajectoryError("player foot point lies outside the source frame")
        connection.execute("INSERT INTO player_feet VALUES (?, ?, ?)", (frame, x, y))
        sample_count += 1
    return {"status": "loaded", "reason": "source_bound_tracks", "sample_count": sample_count}


def _optional_top_level_value(path: Path, key: str) -> Any:
    iterator = _stream_items(path, key)
    try:
        value = next(iterator)
    except StopIteration:
        return None
    try:
        next(iterator)
    except StopIteration:
        return value
    raise GlobalBallTrajectoryError(f"duplicate top-level JSON key: {key}")


def _optional_top_level_mapping(path: Path, key: str) -> dict[str, Any] | None:
    value = _optional_top_level_value(path, key)
    return value if isinstance(value, dict) else None


def _lineage_matches(
    lineage: dict[str, Any] | None,
    source_snapshot: _Snapshot,
    metadata: dict[str, Any],
) -> bool:
    if not lineage:
        return False
    try:
        frame_count = lineage.get("frame_count")
        width = lineage.get("width")
        height = lineage.get("height")
        fps = lineage.get("fps")
        if frame_count is None or width is None or height is None or fps is None:
            return False
        return (
            lineage.get("source_video_sha256") == source_snapshot.sha256
            and int(frame_count) == int(metadata["frame_count"])
            and int(width) == int(metadata["width"])
            and int(height) == int(metadata["height"])
            and math.isclose(float(fps), float(metadata["fps"]), rel_tol=0.0, abs_tol=1e-6)
        )
    except (TypeError, ValueError, OverflowError):
        return False


def _parse_polygon(value: Any) -> list[tuple[float, float]] | None:
    if not isinstance(value, list) or len(value) < 3:
        return None
    result: list[tuple[float, float]] = []
    for item in value:
        if isinstance(item, dict):
            raw_x, raw_y = item.get("x"), item.get("y")
        elif isinstance(item, list) and len(item) == 2:
            raw_x, raw_y = item
        else:
            return None
        try:
            result.append((_finite_float(raw_x, "pitch polygon x"), _finite_float(raw_y, "pitch polygon y")))
        except GlobalBallTrajectoryError:
            return None
    return result


def _prepare_candidate_costs(
    connection: sqlite3.Connection,
    config: TrajectoryConfig,
    priors: dict[str, Any],
) -> None:
    pitch_polygon = None
    if priors["pitch"].get("status") == "loaded":
        pitch_polygon = [tuple(point) for point in priors["pitch"]["polygon"]]

    cursor = connection.execute(
        """
        SELECT candidate_id, frame_index, x, y, detector_confidence, match_probability
        FROM candidates ORDER BY frame_index, candidate_id
        """
    )
    for candidate_id, frame_index, x, y, detector_confidence, match_probability in cursor:
        if match_probability is None:
            raise GlobalBallTrajectoryError("candidate is missing a complete prediction")
        human_rows = connection.execute(
            """
            SELECT label FROM classifications
            WHERE candidate_id = ? AND label_origin = 'human_confirmed'
            ORDER BY label
            """,
            (candidate_id,),
        ).fetchall()
        human_labels = {row[0] for row in human_rows}
        hard_reject_reason = None
        if any(label not in {"match_ball", "unknown"} for label in human_labels):
            hard_reject_reason = "human_confirmed_noise"
        elif float(match_probability) < config.minimum_match_probability:
            hard_reject_reason = "match_probability_below_minimum"

        match_cost = -math.log(max(float(match_probability), 1e-12)) * config.match_probability_weight
        detector_cost = -math.log(max(float(detector_confidence), 1e-12)) * config.detector_confidence_weight
        pitch_cost = 0.0
        if pitch_polygon is not None and not _point_in_polygon(float(x), float(y), pitch_polygon):
            pitch_cost = config.pitch_prior_weight
        player_cost = 0.0
        player_status = priors["player_foot"].get("status")
        nearest_foot = None
        if player_status == "loaded":
            feet = connection.execute(
                "SELECT x, y FROM player_feet WHERE frame_index = ?",
                (frame_index,),
            )
            nearest_foot = min(
                (math.hypot(float(x) - float(foot_x), float(y) - float(foot_y)) for foot_x, foot_y in feet),
                default=None,
            )
            if nearest_foot is not None:
                player_cost = min(1.0, nearest_foot / 200.0) * config.player_foot_weight
        human_bonus = -2.0 if "match_ball" in human_labels else 0.0
        components = {
            "match_ball_probability": match_cost,
            "detector_confidence": detector_cost,
            "pitch": pitch_cost,
            "player_foot": player_cost,
            "human_confirmation": human_bonus,
        }
        if nearest_foot is not None:
            components["nearest_player_foot_distance_px"] = nearest_foot
        node_cost = sum(value for key, value in components.items() if key != "nearest_player_foot_distance_px")
        connection.execute(
            """
            UPDATE candidates
            SET node_cost = ?, node_costs_json = ?, hard_reject_reason = ?
            WHERE candidate_id = ?
            """,
            (node_cost, _canonical_json(components), hard_reject_reason, candidate_id),
        )

    connection.execute(
        """
        WITH ranked AS (
            SELECT candidate_id,
                   ROW_NUMBER() OVER (PARTITION BY frame_index ORDER BY node_cost, candidate_id) AS rank
            FROM candidates
            WHERE hard_reject_reason IS NULL
        )
        UPDATE candidates
        SET budget_pruned = 1
        WHERE candidate_id IN (
            SELECT candidate_id FROM ranked WHERE rank > ?
        )
        """,
        (config.candidate_cap_per_frame,),
    )
    connection.commit()


def _point_in_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    inside = False
    previous = polygon[-1]
    for current in polygon:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > y) != (y2 > y):
            crossing_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing_x:
                inside = not inside
        previous = current
    return inside


def _solve_candidate_graph(
    connection: sqlite3.Connection,
    config: TrajectoryConfig,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    connection.execute("DELETE FROM dp_states")
    connection.execute("DELETE FROM selected")
    frontier: list[_FrontierState] = []
    max_frontier_states = 0
    beam_pruned_count = 0
    candidate_cursor = connection.execute(
        """
        SELECT candidate_id, frame_index, x, y, detector_confidence,
               match_probability, node_cost, node_costs_json
        FROM candidates
        WHERE hard_reject_reason IS NULL AND budget_pruned = 0
        ORDER BY frame_index, node_cost, candidate_id
        """
    )
    for frame_index, raw_rows in groupby(candidate_cursor, key=lambda row: int(row[1])):
        nodes = [
            _CandidateNode(
                candidate_id=str(row[0]),
                frame_index=int(row[1]),
                x=float(row[2]),
                y=float(row[3]),
                detector_confidence=float(row[4]),
                match_probability=float(row[5]),
                node_cost=float(row[6]),
                node_costs=json.loads(row[7]),
            )
            for row in raw_rows
        ]
        proposals: list[_StateProposal] = []
        best_frontier = min(frontier, key=_state_sort_key) if frontier else None
        for node in nodes:
            selection_cost = node.node_cost - config.detection_reward
            proposals.append(
                _StateProposal(
                    node=node,
                    previous=None,
                    total_cost=_quantized(selection_cost),
                    velocity_x=None,
                    velocity_y=None,
                    edge_costs={},
                    restart=False,
                )
            )
            for previous in frontier:
                gap = node.frame_index - previous.frame_index
                if gap <= 0 or gap > config.max_transition_gap:
                    continue
                edge_costs, velocity = _edge_costs(previous, node, gap, metadata, config)
                total = previous.total_cost + selection_cost + sum(edge_costs.values())
                proposals.append(
                    _StateProposal(
                        node=node,
                        previous=previous,
                        total_cost=_quantized(total),
                        velocity_x=velocity[0],
                        velocity_y=velocity[1],
                        edge_costs=edge_costs,
                        restart=False,
                    )
                )
            if best_frontier is not None:
                restart_gap = node.frame_index - best_frontier.frame_index
                restart_cost = config.restart_penalty
                if restart_gap <= config.max_transition_gap:
                    restart_cost += config.adjacent_restart_penalty
                proposals.append(
                    _StateProposal(
                        node=node,
                        previous=best_frontier,
                        total_cost=_quantized(best_frontier.total_cost + selection_cost + restart_cost),
                        velocity_x=None,
                        velocity_y=None,
                        edge_costs={"restart": restart_cost},
                        restart=True,
                    )
                )
        unique: dict[tuple[int, str, float | None, float | None], _FrontierState | _StateProposal] = {}
        for entry in [*frontier, *proposals]:
            key = _frontier_identity(entry)
            current = unique.get(key)
            if current is None or _frontier_entry_sort_key(entry) < _frontier_entry_sort_key(current):
                unique[key] = entry
        ordered = sorted(unique.values(), key=_frontier_entry_sort_key)
        if len(ordered) > config.beam_width:
            beam_pruned_count += len(ordered) - config.beam_width
        kept_entries = ordered[: config.beam_width]
        next_frontier: list[_FrontierState] = []
        kept_current_candidates: set[str] = set()
        for entry in kept_entries:
            if isinstance(entry, _FrontierState):
                next_frontier.append(entry)
                continue
            state = _insert_state(
                connection,
                entry.node,
                entry.previous,
                entry.total_cost,
                entry.velocity_x,
                entry.velocity_y,
                entry.edge_costs,
                entry.restart,
            )
            next_frontier.append(state)
            kept_current_candidates.add(entry.node.candidate_id)
        pruned_candidate_ids = sorted({node.candidate_id for node in nodes} - kept_current_candidates)
        if pruned_candidate_ids:
            connection.executemany(
                "UPDATE candidates SET beam_pruned = 1 WHERE candidate_id = ?",
                ((candidate_id,) for candidate_id in pruned_candidate_ids),
            )
        frontier = next_frontier
        max_frontier_states = max(max_frontier_states, len(frontier))

    if frontier:
        terminal = min(frontier, key=_state_sort_key)
        if terminal.total_cost < 0.0:
            _materialize_selected_path(connection, terminal.state_id)
    connection.commit()
    budget_pruned_count = int(
        connection.execute("SELECT COUNT(*) FROM candidates WHERE budget_pruned = 1").fetchone()[0]
    )
    persisted_state_count = int(connection.execute("SELECT COUNT(*) FROM dp_states").fetchone()[0])
    return {
        "max_frontier_states": max_frontier_states,
        "persisted_state_count": persisted_state_count,
        "beam_pruned_count": beam_pruned_count,
        "candidate_budget_pruned_count": budget_pruned_count,
        "pruned": bool(beam_pruned_count or budget_pruned_count),
    }


def _frontier_identity(
    entry: _FrontierState | _StateProposal,
) -> tuple[int, str, float | None, float | None]:
    if isinstance(entry, _FrontierState):
        return (entry.frame_index, entry.candidate_id, entry.velocity_x, entry.velocity_y)
    return (entry.node.frame_index, entry.node.candidate_id, entry.velocity_x, entry.velocity_y)


def _frontier_entry_sort_key(
    entry: _FrontierState | _StateProposal,
) -> tuple[float, int, str, int]:
    if isinstance(entry, _FrontierState):
        return _state_sort_key(entry)
    previous_state_id = -1 if entry.previous is None else entry.previous.state_id
    return (
        entry.total_cost,
        entry.node.frame_index,
        entry.node.candidate_id,
        previous_state_id,
    )


def _insert_state(
    connection: sqlite3.Connection,
    node: _CandidateNode,
    previous: _FrontierState | None,
    total_cost: float,
    velocity_x: float | None,
    velocity_y: float | None,
    edge_costs: dict[str, float],
    restart: bool,
) -> _FrontierState:
    cursor = connection.execute(
        """
        INSERT INTO dp_states (
            frame_index, candidate_id, previous_state_id, total_cost,
            velocity_x, velocity_y, edge_costs_json, restart
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            node.frame_index,
            node.candidate_id,
            None if previous is None else previous.state_id,
            _quantized(total_cost),
            velocity_x,
            velocity_y,
            _canonical_json(edge_costs),
            int(restart),
        ),
    )
    if cursor.lastrowid is None:
        raise GlobalBallTrajectoryError("trajectory state could not be persisted")
    return _FrontierState(
        state_id=int(cursor.lastrowid),
        frame_index=node.frame_index,
        candidate_id=node.candidate_id,
        x=node.x,
        y=node.y,
        velocity_x=velocity_x,
        velocity_y=velocity_y,
        total_cost=_quantized(total_cost),
    )


def _edge_costs(
    previous: _FrontierState,
    node: _CandidateNode,
    gap: int,
    metadata: dict[str, Any],
    config: TrajectoryConfig,
) -> tuple[dict[str, float], tuple[float, float]]:
    dt = gap / float(metadata["fps"])
    velocity_x = (node.x - previous.x) / dt
    velocity_y = (node.y - previous.y) / dt
    diagonal = math.hypot(float(metadata["width"]), float(metadata["height"]))
    speed_scale = max(diagonal * float(metadata["fps"]), 1e-9)
    speed = math.hypot(velocity_x, velocity_y) / speed_scale
    acceleration = 0.0
    direction = 0.0
    if previous.velocity_x is not None and previous.velocity_y is not None:
        acceleration_scale = max(diagonal * float(metadata["fps"]) ** 2, 1e-9)
        acceleration = (
            math.hypot(velocity_x - previous.velocity_x, velocity_y - previous.velocity_y)
            / dt
            / acceleration_scale
        )
        old_speed = math.hypot(previous.velocity_x, previous.velocity_y)
        new_speed = math.hypot(velocity_x, velocity_y)
        if old_speed > 1e-9 and new_speed > 1e-9:
            cosine = (previous.velocity_x * velocity_x + previous.velocity_y * velocity_y) / (old_speed * new_speed)
            direction = (1.0 - max(-1.0, min(1.0, cosine))) / 2.0
    costs = {
        "speed": _quantized(speed * config.speed_weight),
        "acceleration": _quantized(acceleration * config.acceleration_weight),
        "direction": _quantized(direction * config.direction_weight),
        "gap": _quantized(max(0, gap - 1) * config.gap_weight),
    }
    return costs, (velocity_x, velocity_y)


def _state_sort_key(state: _FrontierState) -> tuple[float, int, str, int]:
    return (state.total_cost, state.frame_index, state.candidate_id, state.state_id)


def _quantized(value: float) -> float:
    return round(float(value), 12)


def _materialize_selected_path(connection: sqlite3.Connection, state_id: int) -> None:
    current: int | None = state_id
    while current is not None:
        row = connection.execute(
            "SELECT frame_index, candidate_id, previous_state_id FROM dp_states WHERE state_id = ?",
            (current,),
        ).fetchone()
        if row is None:
            raise GlobalBallTrajectoryError("trajectory backpointer is missing")
        connection.execute(
            "INSERT OR IGNORE INTO selected VALUES (?, ?, ?)",
            (int(row[0]), str(row[1]), current),
        )
        current = None if row[2] is None else int(row[2])


def _write_outputs(
    connection: sqlite3.Connection,
    staging_dir: Path,
    config: TrajectoryConfig,
    metadata: dict[str, Any],
    work: dict[str, Any],
) -> dict[str, Any]:
    track_path = staging_dir / TRACK_NAME
    decisions_path = staging_dir / DECISIONS_NAME
    status_counts = {status: 0 for status in FRAME_STATUSES}
    row_count = 0
    longest_interpolation = 0
    interpolation_run = 0

    anchors = iter(
        connection.execute(
            """
            SELECT s.frame_index, c.candidate_id, c.x, c.y,
                   c.match_probability, c.detector_confidence, s.state_id, d.restart
            FROM selected AS s
            JOIN candidates AS c ON c.candidate_id = s.candidate_id
            JOIN dp_states AS d ON d.state_id = s.state_id
            ORDER BY s.frame_index
            """
        )
    )
    next_anchor = next(anchors, None)
    previous_anchor = None
    previous_previous_anchor = None

    with track_path.open("x", encoding="utf-8", newline="") as track_handle, decisions_path.open(
        "x", encoding="utf-8", newline="\n"
    ) as decisions_handle:
        writer = csv.DictWriter(
            track_handle,
            fieldnames=[
                "Frame",
                "X",
                "Y",
                "Confidence",
                "Status",
                "SelectedCandidateId",
                "Source",
                "Reason",
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        frame_cursor = connection.execute(
            "SELECT frame_index, status, reason FROM frames ORDER BY frame_index"
        )
        for frame_index, upstream_status, upstream_reason in frame_cursor:
            frame_index = int(frame_index)
            while next_anchor is not None and int(next_anchor[0]) < frame_index:
                previous_previous_anchor = None if bool(next_anchor[7]) else previous_anchor
                previous_anchor = next_anchor
                next_anchor = next(anchors, None)

            selected_anchor = next_anchor if next_anchor is not None and int(next_anchor[0]) == frame_index else None
            if selected_anchor is not None:
                row = _detected_row(selected_anchor)
                previous_previous_anchor = None if bool(selected_anchor[7]) else previous_anchor
                previous_anchor = selected_anchor
                next_anchor = next(anchors, None)
            elif upstream_status == "out_of_view":
                row = _empty_track_row(frame_index, "out_of_view", "explicit_upstream_out_of_view")
            else:
                row = _missing_track_row(
                    frame_index,
                    previous_previous_anchor,
                    previous_anchor,
                    next_anchor,
                    config,
                    metadata,
                )
                if row["Status"] == "unknown" and upstream_reason:
                    row["Reason"] = f"insufficient_global_evidence:{upstream_reason}"
            writer.writerow(row)
            status = row["Status"]
            status_counts[status] += 1
            row_count += 1
            if status == "interpolated":
                interpolation_run += 1
                longest_interpolation = max(longest_interpolation, interpolation_run)
            else:
                interpolation_run = 0
            _write_json_line(
                decisions_handle,
                {
                    "record_type": "frame",
                    "frame_index": frame_index,
                    "status": status,
                    "selected_candidate_id": row["SelectedCandidateId"] or None,
                    "reason": row["Reason"],
                },
            )

        _write_candidate_decisions(connection, decisions_handle)
        track_handle.flush()
        os.fsync(track_handle.fileno())
        decisions_handle.flush()
        os.fsync(decisions_handle.fileno())

    expected_count = int(connection.execute("SELECT COUNT(*) FROM frames").fetchone()[0])
    if row_count != expected_count:
        raise GlobalBallTrajectoryError("ball track row count does not match the contract frame scope")
    return {
        "row_count": row_count,
        "status_counts": status_counts,
        "longest_interpolation_run": longest_interpolation,
        "selected_candidate_count": int(connection.execute("SELECT COUNT(*) FROM selected").fetchone()[0]),
        "candidate_count": int(connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]),
        "work": work,
    }


def _detected_row(anchor: tuple[Any, ...]) -> dict[str, Any]:
    confidence = math.sqrt(max(0.0, float(anchor[4]) * float(anchor[5])))
    return {
        "Frame": int(anchor[0]),
        "X": _format_float(float(anchor[2])),
        "Y": _format_float(float(anchor[3])),
        "Confidence": _format_probability(confidence),
        "Status": "detected",
        "SelectedCandidateId": str(anchor[1]),
        "Source": "global_candidate_graph",
        "Reason": "lowest_evidence_bound_global_path_cost",
    }


def _missing_track_row(
    frame_index: int,
    previous_previous_anchor: tuple[Any, ...] | None,
    previous_anchor: tuple[Any, ...] | None,
    next_anchor: tuple[Any, ...] | None,
    config: TrajectoryConfig,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    if previous_anchor is None:
        return _empty_track_row(frame_index, "unknown", "no_previous_detection_anchor")
    previous_frame = int(previous_anchor[0])
    if next_anchor is not None:
        next_frame = int(next_anchor[0])
        missing_count = next_frame - previous_frame - 1
        if bool(next_anchor[7]):
            return _empty_track_row(frame_index, "unknown", "trajectory_segment_restart_boundary")
        if missing_count <= config.max_interpolation_gap:
            position = _kalman_position_between(previous_anchor, next_anchor, frame_index)
            if not _position_in_frame(position, metadata):
                return _empty_track_row(frame_index, "unknown", "predicted_position_outside_frame")
            distance = frame_index - previous_frame
            confidence = min(_anchor_confidence(previous_anchor), _anchor_confidence(next_anchor)) * (
                config.interpolation_confidence_decay**distance
            )
            return {
                "Frame": frame_index,
                "X": _format_float(position[0]),
                "Y": _format_float(position[1]),
                "Confidence": _format_probability(confidence),
                "Status": "interpolated",
                "SelectedCandidateId": "",
                "Source": "constant_acceleration_kalman",
                "Reason": "bounded_gap_between_global_detection_anchors",
            }
        return _empty_track_row(frame_index, "unknown", "global_detection_gap_exceeds_limit")
    trailing_gap = frame_index - previous_frame
    if trailing_gap <= config.max_interpolation_gap:
        position = _kalman_position_after(previous_previous_anchor, previous_anchor, frame_index)
        if not _position_in_frame(position, metadata):
            return _empty_track_row(frame_index, "unknown", "predicted_position_outside_frame")
        confidence = _anchor_confidence(previous_anchor) * (config.interpolation_confidence_decay**trailing_gap)
        return {
            "Frame": frame_index,
            "X": _format_float(position[0]),
            "Y": _format_float(position[1]),
            "Confidence": _format_probability(confidence),
            "Status": "interpolated",
            "SelectedCandidateId": "",
            "Source": "constant_acceleration_kalman",
            "Reason": "bounded_trailing_prediction",
        }
    return _empty_track_row(frame_index, "unknown", "trailing_prediction_limit_reached")


def _anchor_confidence(anchor: tuple[Any, ...]) -> float:
    return math.sqrt(max(0.0, float(anchor[4]) * float(anchor[5])))


def _position_in_frame(position: tuple[float, float], metadata: dict[str, Any]) -> bool:
    return (
        math.isfinite(position[0])
        and math.isfinite(position[1])
        and 0.0 <= position[0] < float(metadata["width"])
        and 0.0 <= position[1] < float(metadata["height"])
    )


def _kalman_position_between(
    previous_anchor: tuple[Any, ...],
    next_anchor: tuple[Any, ...],
    frame_index: int,
) -> tuple[float, float]:
    frame_gap = int(next_anchor[0]) - int(previous_anchor[0])
    velocity = (
        (float(next_anchor[2]) - float(previous_anchor[2])) / frame_gap,
        (float(next_anchor[3]) - float(previous_anchor[3])) / frame_gap,
    )
    kalman = ConstantAccelerationKalmanFilter()
    kalman.initialize((float(previous_anchor[2]), float(previous_anchor[3])), velocity=velocity)
    kalman.predict(dt=float(frame_index - int(previous_anchor[0])))
    return kalman.get_position()


def _kalman_position_after(
    previous_previous_anchor: tuple[Any, ...] | None,
    previous_anchor: tuple[Any, ...],
    frame_index: int,
) -> tuple[float, float]:
    velocity = (0.0, 0.0)
    if previous_previous_anchor is not None:
        gap = int(previous_anchor[0]) - int(previous_previous_anchor[0])
        if gap > 0:
            velocity = (
                (float(previous_anchor[2]) - float(previous_previous_anchor[2])) / gap,
                (float(previous_anchor[3]) - float(previous_previous_anchor[3])) / gap,
            )
    kalman = ConstantAccelerationKalmanFilter()
    kalman.initialize((float(previous_anchor[2]), float(previous_anchor[3])), velocity=velocity)
    kalman.predict(dt=float(frame_index - int(previous_anchor[0])))
    return kalman.get_position()


def _empty_track_row(frame_index: int, status: str, reason: str) -> dict[str, Any]:
    return {
        "Frame": frame_index,
        "X": "",
        "Y": "",
        "Confidence": "",
        "Status": status,
        "SelectedCandidateId": "",
        "Source": "global_candidate_graph",
        "Reason": reason,
    }


def _write_candidate_decisions(connection: sqlite3.Connection, handle: Any) -> None:
    cursor = connection.execute(
        """
        SELECT c.frame_index, c.candidate_id, c.node_costs_json, c.node_cost,
               c.hard_reject_reason, c.budget_pruned, c.beam_pruned,
               s.state_id, d.total_cost, d.edge_costs_json, d.restart,
               alternative.total_cost AS counterfactual_path_cost,
               alternative.edge_costs_json AS counterfactual_edge_costs,
               alternative.restart AS counterfactual_restart,
               selected_path.total_cost AS selected_frame_path_cost
        FROM candidates AS c
        LEFT JOIN selected AS s ON s.candidate_id = c.candidate_id
        LEFT JOIN dp_states AS d ON d.state_id = s.state_id
        LEFT JOIN dp_states AS alternative ON alternative.state_id = (
            SELECT best.state_id
            FROM dp_states AS best
            WHERE best.candidate_id = c.candidate_id
            ORDER BY best.total_cost, best.state_id
            LIMIT 1
        )
        LEFT JOIN selected AS selected_frame ON selected_frame.frame_index = c.frame_index
        LEFT JOIN dp_states AS selected_path ON selected_path.state_id = selected_frame.state_id
        ORDER BY c.frame_index, c.candidate_id
        """
    )
    for row in cursor:
        frame_index = int(row[0])
        hard_reason = row[4]
        budget_pruned = bool(row[5])
        beam_pruned = bool(row[6])
        selected = row[7] is not None
        if selected:
            decision = "selected"
            reason = "lowest_evidence_bound_global_path_cost"
        elif hard_reason:
            decision = "rejected"
            reason = str(hard_reason)
        elif budget_pruned:
            decision = "rejected"
            reason = "candidate_budget_exceeded"
        elif beam_pruned:
            decision = "rejected"
            reason = "beam_path_pruned"
        else:
            decision = "rejected"
            reason = "higher_global_path_cost"
        payload: dict[str, Any] = {
            "record_type": "candidate",
            "frame_index": frame_index,
            "candidate_id": row[1],
            "decision": decision,
            "reason": reason,
            "costs": {
                "node": json.loads(row[2]) if row[2] else None,
                "edge": json.loads(row[9] if selected else row[12]) if (row[9] if selected else row[12]) else {},
                "path_total": (
                    None
                    if (row[8] if selected else row[11]) is None
                    else float(row[8] if selected else row[11])
                ),
            },
        }
        counterfactual_path_cost = row[11]
        selected_path_cost = row[14]
        if not selected and counterfactual_path_cost is not None and selected_path_cost is not None:
            payload["counterfactual_delta"] = _quantized(
                max(0.0, float(counterfactual_path_cost) - float(selected_path_cost))
            )
        if selected:
            payload["restart"] = bool(row[10])
        elif counterfactual_path_cost is not None:
            payload["counterfactual_restart"] = bool(row[13])
        _write_json_line(handle, payload)
    for candidate_id, decision, confidence, reason in connection.execute(
        "SELECT candidate_id, decision, confidence, reason FROM raw_decisions ORDER BY candidate_id, decision"
    ):
        _write_json_line(
            handle,
            {
                "record_type": "unvalidated_selective_decision",
                "candidate_id": candidate_id,
                "decision": "ignored",
                "reason": "unvalidated_selective_decision",
                "source_decision": decision,
                "source_confidence": confidence,
                "source_reason": reason,
            },
        )


def _build_report(
    staging_dir: Path,
    snapshots: list[_Snapshot],
    metadata: dict[str, Any],
    prediction_metadata: dict[str, Any],
    config: TrajectoryConfig,
    priors: dict[str, Any],
    work: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    artifacts = {}
    for name in (TRACK_NAME, DECISIONS_NAME):
        path = staging_dir / name
        artifacts[name] = {"sha256": _sha256_file(path), "size": path.stat().st_size}
    input_bindings = {
        snapshot.label: {
            "path": str(snapshot.path),
            "sha256": snapshot.sha256,
            "size": snapshot.size,
        }
        for snapshot in snapshots
    }
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "artifact_type": "global_ball_trajectory_report",
        "status": "succeeded",
        "complete": True,
        "algorithm": {
            "version": ALGORITHM_VERSION,
            "state_order": 2,
            "tie_break": "quantized_total_cost_then_frame_then_candidate_id",
            "pruned": work["pruned"],
            "optimality": "beam_approximation" if work["pruned"] else "bounded_graph_exact",
            "configuration": asdict(config),
        },
        "source_video": {"sha256": input_bindings["source video"]["sha256"], **metadata},
        "inputs": input_bindings,
        "predictions": prediction_metadata,
        "priors": {
            "pitch": {key: value for key, value in priors["pitch"].items() if key != "polygon"},
            "player_foot": priors["player_foot"],
        },
        "work": work,
        "summary": {key: value for key, value in summary.items() if key != "work"},
        "artifacts": artifacts,
    }


def _format_float(value: float) -> str:
    return f"{value:.6f}"


def _format_probability(value: float) -> str:
    return f"{max(0.0, min(1.0, value)):.6f}"


def _write_json_line(handle: Any, value: dict[str, Any]) -> None:
    handle.write(_canonical_json(value) + "\n")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_json_commit(path: Path, value: dict[str, Any]) -> None:
    if path.exists():
        raise GlobalBallTrajectoryError("trajectory commit report already exists")
    pending = path.parent / f".{path.name}.pending-{uuid4().hex}"
    try:
        _write_json(pending, value)
        os.replace(pending, path)
        _fsync_directory(path.parent)
    finally:
        try:
            pending.unlink(missing_ok=True)
        except OSError:
            pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _lock_path(output_dir: Path) -> Path:
    scope = hashlib.sha256(str(output_dir).casefold().encode("utf-8")).hexdigest()
    return Path(tempfile.gettempdir()) / f"football-tracking-global-trajectory-{scope}.lock"


def _acquire_output_lock(output_dir: Path) -> BinaryIO:
    path = _lock_path(output_dir)
    handle = path.open("a+b")
    try:
        handle.seek(0)
        if not handle.read(1):
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException as exc:
        handle.close()
        raise GlobalBallTrajectoryError(f"trajectory output is already locked: {output_dir}") from exc
    return handle


def _release_output_lock(handle: BinaryIO) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _publish_generation(staging_dir: Path, output_dir: Path) -> None:
    if output_dir.exists():
        raise GlobalBallTrajectoryError("immutable trajectory generation already exists")
    published = False
    try:
        os.replace(staging_dir, output_dir)
        published = True
        _fsync_directory(output_dir.parent)
    except BaseException:
        if published and output_dir.exists():
            _discard_published_generation(output_dir)
        raise


def _discard_published_generation(output_dir: Path) -> None:
    if not output_dir.exists():
        return
    quarantine = output_dir.parent / f".{output_dir.name}.invalid-{uuid4().hex}"
    try:
        os.replace(output_dir, quarantine)
    except OSError:
        shutil.rmtree(output_dir, ignore_errors=True)
        return
    shutil.rmtree(quarantine, ignore_errors=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
