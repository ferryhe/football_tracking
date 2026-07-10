from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NoReturn


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(f"argument error: {message}")


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from football_tracking.camera_path_renderer import CameraPathRenderError, render_camera_path_video

    parser = _JsonArgumentParser(description="Render a verified hybrid broadcast camera path.")
    parser.add_argument("--source-video", required=True, type=Path)
    parser.add_argument("--camera-path", required=True, type=Path)
    parser.add_argument("--hybrid-report", required=True, type=Path)
    parser.add_argument("--output-video", required=True, type=Path)
    parser.add_argument("--target-width", type=int, default=1920)
    parser.add_argument("--target-height", type=int, default=1080)
    parser.add_argument("--codec", default="mp4v")
    try:
        args = parser.parse_args(argv)
        result = render_camera_path_video(
            args.source_video,
            args.camera_path,
            args.hybrid_report,
            args.output_video,
            target_width=args.target_width,
            target_height=args.target_height,
            codec=args.codec,
        )
    except KeyboardInterrupt:
        print(
            json.dumps({"ok": False, "error": {"type": "KeyboardInterrupt", "message": "cancelled"}}),
            file=sys.stderr,
        )
        return 130
    except (CameraPathRenderError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "frame_count": result.frame_count,
                "target_resolution": [result.target_width, result.target_height],
                "output_video": str(result.output_video_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
