from __future__ import annotations

import math
from pathlib import Path
from typing import Any

LOW_VARIANCE_STD = 6.0
LOW_TEXTURE_TILE_RATIO = 0.10
GRAY_CHANNEL_DELTA = 4.0
DOMINANT_COLOR_RATIO = 0.74
GRAY_DOMINANT_COLOR_RATIO = 0.50
TILE_GRID = 8


def inspect_image(path: Path) -> dict[str, Any]:
    """Return lightweight integrity signals for AI review images."""
    path = Path(path)
    result = _empty_inspection_result(path=str(path))
    if not path.exists() or path.stat().st_size <= 0:
        result["likely_corrupt"] = True
        result["reasons"] = ["missing_or_empty"]
        return result

    import cv2

    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None or getattr(image, "size", 0) == 0:
        result["likely_corrupt"] = True
        result["reasons"] = ["unreadable"]
        return result

    result.update(inspect_frame(image))
    result["path"] = str(path)
    return result


def inspect_frame(frame: Any) -> dict[str, Any]:
    """Return the same low-information signals for an already decoded BGR frame."""
    result = _empty_inspection_result(path=None)
    if frame is None or getattr(frame, "size", 0) == 0:
        result["likely_corrupt"] = True
        result["reasons"] = ["unreadable"]
        return result

    import cv2
    import numpy as np

    height, width = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    mean_luma = float(np.mean(gray))
    std_luma = float(np.std(gray))
    texture_ratio = _texture_tile_ratio(gray)
    channel_delta = _mean_channel_delta(frame)
    dominant_color_ratio = _dominant_color_ratio(frame)
    reasons: list[str] = []
    if std_luma < LOW_VARIANCE_STD:
        reasons.append("low_variance")
    if texture_ratio < LOW_TEXTURE_TILE_RATIO:
        reasons.append("low_texture")
    if dominant_color_ratio >= DOMINANT_COLOR_RATIO:
        reasons.append("dominant_flat_color")
    is_gray = channel_delta <= GRAY_CHANNEL_DELTA
    if is_gray:
        reasons.append("gray_palette")
        if dominant_color_ratio >= GRAY_DOMINANT_COLOR_RATIO:
            reasons.append("gray_dominant_palette")

    low_information = bool(
        "low_variance" in reasons
        or "low_texture" in reasons
        or "dominant_flat_color" in reasons
        or "gray_dominant_palette" in reasons
        or ("gray_palette" in reasons and ("low_variance" in reasons or "low_texture" in reasons))
    )
    result.update(
        {
            "status": "ok",
            "width": int(width),
            "height": int(height),
            "mean_luma": round(mean_luma, 3),
            "std_luma": round(std_luma, 3),
            "texture_tile_ratio": round(texture_ratio, 3),
            "dominant_color_ratio": round(dominant_color_ratio, 3),
            "gray": is_gray,
            "low_information": low_information,
            "likely_corrupt": False,
            "reasons": reasons,
        }
    )
    return result


def _empty_inspection_result(*, path: str | None) -> dict[str, Any]:
    return {
        "path": path,
        "status": "unavailable",
        "width": 0,
        "height": 0,
        "mean_luma": 0.0,
        "std_luma": 0.0,
        "texture_tile_ratio": 0.0,
        "dominant_color_ratio": 0.0,
        "gray": False,
        "low_information": False,
        "likely_corrupt": False,
        "reasons": [],
    }


def inspect_named_images(images: dict[str, Path]) -> tuple[dict[str, Any], list[str]]:
    integrity: dict[str, Any] = {}
    warnings: list[str] = []
    for label, path in images.items():
        result = inspect_image(path)
        integrity[label] = result
        if result.get("likely_corrupt"):
            warnings.append(f"{label}_unreadable")
        elif result.get("low_information"):
            warnings.append(f"{label}_low_information")
    return integrity, warnings


def summarize_media_integrity(items: list[dict[str, Any]]) -> dict[str, Any]:
    images = [
        image
        for item in items
        for image in _iter_integrity_images(item.get("media_integrity"))
    ]
    low_information_count = sum(1 for image in images if image.get("low_information"))
    likely_corrupt_count = sum(1 for image in images if image.get("likely_corrupt"))
    return {
        "status": "warn" if low_information_count or likely_corrupt_count else "ok",
        "image_count": len(images),
        "low_information_image_count": low_information_count,
        "likely_corrupt_image_count": likely_corrupt_count,
        "gray_image_count": sum(1 for image in images if image.get("gray")),
        "warning_labels": sorted(
            {
                reason
                for image in images
                for reason in image.get("reasons", [])
                if isinstance(reason, str)
            }
        ),
    }


def review_source_provenance(input_video: Path | None) -> dict[str, Any]:
    resolved = input_video.resolve() if input_video is not None else None
    input_text = str(resolved) if resolved is not None else None
    used_review_friendly_source = bool(
        resolved is not None and "review_source" in resolved.name.casefold()
    )
    fallback_command = None
    if input_text is not None:
        output_path = resolved.with_name(f"{resolved.stem}.review_source.mp4")
        fallback_command = (
            "python python_backend/scripts/transcode_review_source.py "
            f"--input-video \"{input_text}\" --output-video \"{output_path}\""
        )
    return {
        "input_video": input_text,
        "used_review_friendly_source": used_review_friendly_source,
        "fallback_command": fallback_command,
        "fallback_reason": "Use a sequential mp4v review source when HEVC/random-seek sheets are gray or low-information.",
    }


def transcode_review_source(input_video: Path, output_video: Path) -> dict[str, Any]:
    input_video = Path(input_video)
    output_video = Path(output_video)
    if input_video.resolve() == output_video.resolve():
        return _transcode_result(input_video, output_video, status="error", reason="output video must differ from input video")

    capture = None
    writer = None
    frames_written = 0
    try:
        output_video.parent.mkdir(parents=True, exist_ok=True)
        import cv2

        capture = cv2.VideoCapture(str(input_video))
        if not capture.isOpened():
            return _transcode_result(input_video, output_video, status="unavailable", reason="input video could not be opened")
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 20.0)
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if width <= 0 or height <= 0:
            return _transcode_result(input_video, output_video, status="unavailable", reason="input video dimensions unavailable")

        writer = cv2.VideoWriter(str(output_video), cv2.VideoWriter.fourcc(*"mp4v"), fps, (width, height))
        if not writer.isOpened():
            return _transcode_result(input_video, output_video, status="error", reason="output writer could not be opened")

        while True:
            ok, frame = capture.read()
            if not ok:
                break
            writer.write(frame)
            frames_written += 1
    except Exception as exc:
        if writer is not None:
            writer.release()
            writer = None
        if capture is not None:
            capture.release()
            capture = None
        output_video.unlink(missing_ok=True)
        return _transcode_result(input_video, output_video, status="error", reason=f"{exc.__class__.__name__}: {exc}")
    finally:
        if writer is not None:
            writer.release()
        if capture is not None:
            capture.release()

    if frames_written <= 0 or not output_video.exists() or output_video.stat().st_size <= 0:
        output_video.unlink(missing_ok=True)
        return _transcode_result(input_video, output_video, status="error", reason="no frames were written")
    return _transcode_result(
        input_video,
        output_video,
        status="ok",
        width=width,
        height=height,
        fps=fps if math.isfinite(fps) and fps > 0 else 0.0,
        frames_written=frames_written,
    )


def _iter_integrity_images(media_integrity: Any) -> list[dict[str, Any]]:
    if not isinstance(media_integrity, dict):
        return []
    return [value for value in media_integrity.values() if isinstance(value, dict)]


def _texture_tile_ratio(gray: Any) -> float:
    height, width = gray.shape[:2]
    if height <= 0 or width <= 0:
        return 0.0
    textured = 0
    total = 0
    for row in range(TILE_GRID):
        y0 = int(round(row * height / TILE_GRID))
        y1 = int(round((row + 1) * height / TILE_GRID))
        for col in range(TILE_GRID):
            x0 = int(round(col * width / TILE_GRID))
            x1 = int(round((col + 1) * width / TILE_GRID))
            tile = gray[y0:y1, x0:x1]
            if getattr(tile, "size", 0) == 0:
                continue
            total += 1
            if float(tile.std()) >= LOW_VARIANCE_STD:
                textured += 1
    return 0.0 if total == 0 else textured / total


def _mean_channel_delta(image: Any) -> float:
    import numpy as np

    blue = image[:, :, 0].astype("float32")
    green = image[:, :, 1].astype("float32")
    red = image[:, :, 2].astype("float32")
    return float(np.mean((np.abs(red - green) + np.abs(red - blue) + np.abs(green - blue)) / 3.0))


def _dominant_color_ratio(image: Any) -> float:
    import numpy as np

    quantized = (image // 16).reshape(-1, 3)
    if quantized.size == 0:
        return 0.0
    _colors, counts = np.unique(quantized, axis=0, return_counts=True)
    return float(counts.max() / counts.sum())


def _transcode_result(
    input_video: Path,
    output_video: Path,
    *,
    status: str,
    reason: str | None = None,
    width: int = 0,
    height: int = 0,
    fps: float = 0.0,
    frames_written: int = 0,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "input_video": str(input_video.resolve()),
        "review_source_video": str(output_video.resolve()),
        "codec": "mp4v",
        "width": int(width),
        "height": int(height),
        "fps": round(float(fps), 3),
        "frames_written": int(frames_written),
    }
    if reason:
        result["reason"] = reason
    return result
