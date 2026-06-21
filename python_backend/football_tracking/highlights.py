from __future__ import annotations

from concurrent.futures import CancelledError
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import cv2


def render_highlight_clip(
    *,
    input_video: Path,
    output_path: Path,
    start_frame: int,
    end_frame: int,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    if start_frame < 0 or end_frame < start_frame:
        raise ValueError(f"Invalid highlight frame window: {start_frame}-{end_frame}")

    capture = cv2.VideoCapture(str(input_video))
    if not capture.isOpened():
        raise RuntimeError(f"Unable to open input video for highlight render: {input_video}")

    fps = capture.get(cv2.CAP_PROP_FPS) or 20.0
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if width <= 0 or height <= 0:
        capture.release()
        raise RuntimeError(f"Unable to read source video dimensions: {input_video}")

    total_source_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if total_source_frames > 0 and (start_frame >= total_source_frames or end_frame >= total_source_frames):
        capture.release()
        raise ValueError(
            f"Highlight frame window {start_frame}-{end_frame} is outside source video length "
            f"{total_source_frames}."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f".{output_path.stem}.{uuid4().hex}.tmp{output_path.suffix}")
    writer = cv2.VideoWriter(
        str(temp_path),
        cv2.VideoWriter.fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    if not writer.isOpened():
        capture.release()
        with suppress(OSError):
            temp_path.unlink()
        raise RuntimeError(f"Unable to open highlight writer: {output_path}")

    requested_count = end_frame - start_frame + 1
    written_count = 0
    try:
        try:
            capture.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
            _emit_progress(progress_callback, current_frame=0, total_frames=requested_count)
            for frame_index in range(start_frame, end_frame + 1):
                if should_cancel is not None and should_cancel():
                    raise CancelledError("Run cancelled by user.")
                ok, frame = capture.read()
                if not ok:
                    break
                writer.write(frame)
                written_count += 1
                _emit_progress(progress_callback, current_frame=written_count, total_frames=requested_count)
        finally:
            writer.release()
            capture.release()
    except BaseException:
        with suppress(OSError):
            temp_path.unlink()
        raise

    if written_count == 0:
        with suppress(OSError):
            temp_path.unlink()
        raise RuntimeError(f"Highlight window contains no readable frames: {start_frame}-{end_frame}")

    temp_path.replace(output_path)
    return {
        "frame_count": written_count,
        "fps": round(float(fps), 4),
        "resolution": [width, height],
        "requested_start_frame": start_frame,
        "requested_end_frame": end_frame,
    }


def _emit_progress(
    progress_callback: Callable[[dict[str, Any]], None] | None,
    *,
    current_frame: int,
    total_frames: int,
) -> None:
    if progress_callback is None:
        return
    progress_callback(
        {
            "stage": "render",
            "current_frame": current_frame,
            "total_frames": total_frames,
        }
    )
