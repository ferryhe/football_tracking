# High-Resolution Football Ball Tracking

这是一个面向本地 Windows 环境的工程化足球比赛用球唯一追踪项目，针对 `5210 x 1440 / 20 FPS` 广角视频设计。系统严格采用五层架构：

1. Detection Layer：YOLO + SAHI，只输出候选球。
2. Candidate Filtering Layer：只做置信度、尺寸、长宽比、ROI 等基础过滤。
3. Selection Layer：基于轨迹连续性和物理约束，从多个候选中选出唯一比赛用球。
4. Tracking Layer：状态机管理 `INIT / TRACKING / PREDICTING / LOST`，支持短时预测。
5. Output Layer：输出视频、逐帧图片和 `ball_track.csv`。

## 项目结构

```text
foot_ball_tracking/
├─ config/
│  └─ default.yaml
├─ football_tracking/
│  ├─ __init__.py
│  ├─ config.py
│  ├─ detector.py
│  ├─ exporter.py
│  ├─ filtering.py
│  ├─ physics.py
│  ├─ pipeline.py
│  ├─ renderer.py
│  ├─ selector.py
│  ├─ tracker.py
│  └─ types.py
├─ main.py
├─ README.md
└─ requirements.txt
```

## 环境要求

- Windows 10 / 11
- Python 3.10 或 3.11
- NVIDIA RTX 4060 / 5060 8G 级别显卡
- 已正确安装 NVIDIA 驱动、CUDA Runtime 和 cuDNN

## 安装步骤

1. 创建虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
```

2. 先安装与本机 CUDA 版本匹配的 GPU 版 PyTorch。

示例（CUDA 12.4）：

```powershell
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
```

3. 安装项目依赖：

```powershell
pip install -r requirements.txt
```

## 数据准备

按如下方式放置数据：

```text
foot_ball_tracking/
├─ data/
│  └─ input.mp4
└─ weights/
   └─ football_ball_yolo.pt
```

如果路径不同，直接修改 `config/default.yaml`。

## 运行命令

```powershell
python main.py --config config/default.yaml
```

## 输出说明

默认输出目录为 `outputs/run_001/`，包含：

- `annotated.mp4`：黄色圆圈标注后的结果视频
- `frames/`：逐帧图片
- `ball_track.csv`：轨迹 CSV，列为 `Frame, X, Y, Confidence, Status`
- `debug.jsonl`：逐帧调试信息，便于分析候选数量、选中原因和状态切换

`Status` 只会输出以下三种值：

- `Detected`
- `Predicted`
- `Lost`

## 配置建议

- 显存紧张时优先减小 `slice_width`、`slice_height`，并降低重叠比例。
- 轨迹跳跃明显时优先调小 `match_distance`、`max_speed`、`max_acceleration`。
- 遮挡恢复能力不足时适度提高 `max_lost_frames`。
- 误跟静态海报或非比赛球时，优先收紧候选尺寸范围和速度/加速度上限。

## 鲁棒性设计

- 单帧检测失败不会导致整体中断，会退化为 `Predicted` 或 `Lost`
- 检测与追踪严格解耦，避免状态逻辑污染检测层
- 所有关键参数集中在配置文件，便于不同视频快速调参
- 调试日志保留每帧候选数量、评分结果、选中原因、状态和丢失计数

## Repo Skill

仓库内已经附带当前项目使用的 Codex skill，路径如下：

```text
skills/
└─ high-resolution-football-ball-tracking-system-designer/
   ├─ SKILL.md
   └─ agents/
      └─ openai.yaml
```

如果你在其他机器上继续开发，可以把该目录复制到本机的 `$CODEX_HOME/skills/` 下。例如在 Windows 上：

```powershell
Copy-Item -Recurse -Force .\skills\high-resolution-football-ball-tracking-system-designer $env:USERPROFILE\.codex\skills\
```

复制后即可通过 `$high-resolution-football-ball-tracking-system-designer` 调用同一套技能约束继续开发。
