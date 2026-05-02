from __future__ import annotations

import argparse
from pathlib import Path

from football_tracking.config import load_config
from football_tracking.pipeline import BallTrackingPipeline


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="足球比赛用球唯一追踪系统")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/default.yaml"),
        help="配置文件路径",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="启用 mock detector / 假数据联调模式",
    )
    parser.add_argument(
        "--mock-scenario",
        choices=["A", "B", "C"],
        help="指定 mock 场景，传入后会自动启用 mock 模式",
    )
    return parser.parse_args()


def main() -> None:
    """程序唯一入口。"""
    args = parse_args()
    config = load_config(args.config)
    if args.mock:
        config.mock.enabled = True
    if args.mock_scenario:
        config.mock.enabled = True
        config.mock.scenario = args.mock_scenario
    pipeline = BallTrackingPipeline(config)
    pipeline.run()


if __name__ == "__main__":
    main()
