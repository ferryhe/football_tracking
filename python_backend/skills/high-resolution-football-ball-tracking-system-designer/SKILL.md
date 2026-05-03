---
name: high-resolution-football-ball-tracking-system-designer
description: Design and implement an engineering-grade unique match-ball tracking system for football or soccer wide high-resolution video, especially when building, refactoring, debugging, reviewing, or optimizing YOLO + SAHI pipelines such as 5210x1440 broadcast footage. Use for tasks involving detector.py, selector.py, tracker.py, physics.py, renderer.py, exporter.py, temporal consistency, scoring-based unique-ball selection, short-term prediction, and robust debug or output tooling.
---

# High-Resolution Football Ball Tracking System Designer

## Core Mandate

- 先设计架构，再写代码。
- 严格拆成 5 层，不要合并职责。
- 将 Detection 和 Tracking 完全解耦。
- 将“唯一比赛用球”定义为“轨迹最合理且运动最连续的目标”，不是“置信度最高的目标”。
- 优先保证时间一致性，允许短时误检，但不允许轨迹跳跃。
- 在关键逻辑附近写中文解释，尤其是 selection、tracking、physics。
- 不要把所有逻辑塞进一个大循环；拆成模块、类、函数和清晰的数据流。

## Delivery Workflow

1. 先定义分层架构、数据结构、配置对象和模块接口。
2. 再实现检测层，只输出原始候选球。
3. 再实现候选过滤层，只做基础过滤。
4. 再实现唯一目标选择层，将历史轨迹和物理约束作为核心。
5. 再实现状态机追踪层，负责状态、历史、速度、加速度和短期预测。
6. 最后实现输出层、调试信息、CSV 和绘制逻辑。
7. 在完成实现后，检查系统是否能过滤静态海报球、误检球、场外球和短轨迹非比赛球。

## Required Five-Layer Architecture

### 1. Detection Layer
- 使用 YOLO + SAHI。
- 只负责在当前帧输出候选球列表。
- 不在此层做状态判断、轨迹关联、丢失恢复或唯一球选择。
- 允许输出多个候选，宁可召回高一些，也不要在此层硬编码追踪逻辑。

### 2. Candidate Filtering Layer
- 只做基础过滤：
- 置信度阈值。
- 尺寸范围。
- 长宽比范围。
- ROI 预留接口。
- 输出“合理候选集合”，不要在此层做唯一球判定。

### 3. Selection Layer
- 从多个合理候选中选出唯一比赛用球。
- 必须综合以下信息：
- 与上一帧位置或预测点的距离。
- 轨迹连续性。
- 速度合理性。
- 加速度合理性。
- 历史轨迹长度。
- 置信度只作为低权重辅助项。
- 禁止仅按 confidence 排序后直接选第一名。

### 4. Tracking Layer
- 实现状态机：`INIT`、`TRACKING`、`PREDICTING`、`LOST`。
- 维护轨迹历史、速度、加速度、丢失计数、最近命中帧。
- 在短时无匹配时优先进入 `PREDICTING`，而不是立刻 `LOST`。
- 支持短期预测，至少实现匀速模型 `(x, y, vx, vy)`，或使用 Kalman Filter。

### 5. Output Layer
- 绘制视频输出，使用黄色圆圈标记球位置。
- 在左下角显示帧号。
- 输出逐帧图片。
- 输出 CSV。

## Required Module Split

- `detector.py`
  只负责模型初始化、SAHI 推理、候选框标准化输出。
- `selector.py`
  只负责给候选打分并选出唯一目标。
- `tracker.py`
  只负责状态机、轨迹历史、预测和丢失恢复。
- `physics.py`
  只负责速度、加速度、方向、距离、预测等物理量计算。
- `renderer.py`
  只负责绘制。
- `exporter.py`
  只负责 CSV 和逐帧输出。

如果现有仓库结构不同，先对接口做适配，再逐步重构到上述职责边界。

## Selection Rules

对每个候选都计算综合评分，并显式保留打分明细，便于 debug。

使用以下公式作为默认形态：

```text
score =
    w1 * distance_score +
    w2 * direction_score +
    w3 * velocity_score +
    w4 * acceleration_penalty +
    w5 * trajectory_length_bonus +
    w6 * confidence
```

按以下原则实现：

- `distance_score`：越接近预测点或上一帧合理延伸位置越高。
- `direction_score`：方向变化越连续越高；突然反向或大幅偏转要降分。
- `velocity_score`：速度越接近历史合理区间越高。
- `acceleration_penalty`：突发速度跃迁、非物理加速度要惩罚。
- `trajectory_length_bonus`：历史越长、连续命中越稳定，可信度越高。
- `confidence`：仅作为辅助项，权重要低。

优先顺序如下：

1. 时间连续性。
2. 物理合理性。
3. 轨迹稳定性。
4. 模型置信度。

如果当前帧存在多个高分候选，优先选择与历史轨迹更一致的目标；不要为了追求单帧检测最强置信度而切换轨迹归属。

## Tracking Rules

按状态机实现追踪逻辑：

- `INIT`
  尚未形成稳定轨迹。允许建立初始轨迹，但不要过早确认长期目标。
- `TRACKING`
  当前帧成功匹配到合理候选，更新位置、速度、加速度和历史轨迹。
- `PREDICTING`
  当前帧未命中，但 `lost_frames <= threshold`。使用预测位置维持短期连续性。
- `LOST`
  连续丢失超过阈值，停止将预测点视为有效比赛球。

在没有匹配候选时，遵守以下逻辑：

```text
if lost_frames <= max_lost_frames:
    use predicted position
    state = PREDICTING
else:
    state = LOST
```

短期预测至少支持以下之一：

- 匀速模型：使用上一时刻位置和速度外推。
- Kalman Filter：使用状态估计平滑短时遮挡和噪声。

## Physics Rules

- 显式维护位置、速度、加速度。
- 将速度和加速度约束做成可调参数，不要写死在选择逻辑里。
- 用物理量过滤以下错误目标：
- 静态海报上的足球图案：速度长期接近 0。
- 场外飞来的球：与既有轨迹不连续。
- 突发误检：突然出现又消失。
- 非比赛球：轨迹过短、连续性弱、缺乏历史支撑。

当模型输出和物理规律冲突时，优先相信物理约束和时间连续性。

## Implementation Requirements

- 暴露可调参数：
- SAHI slice 参数。
- `max_lost_frames`。
- `max_speed`。
- `max_acceleration`。
- `match_distance`。
- 在配置对象或配置文件中集中管理这些参数。
- 避免重复初始化模型；模型对象应复用。
- 将 IO、推理、追踪、渲染解耦，避免单个函数承担全部工作。
- 对单帧异常、空检测、写文件失败等情况做异常保护，保证流程不中断。

## Debug Requirements

至少输出以下调试信息，便于分析为什么选中或丢失目标：

- 每帧候选数量。
- 过滤后候选数量。
- 每个候选的打分明细。
- 最终选中原因。
- 当前状态。
- `lost_frames`。
- 预测位置与真实匹配位置差异。

如果用户要求 debug 模式，优先以结构化日志或可选 verbose 输出实现，而不是散落的 `print`。

## Performance Rules

- 优先使用 CUDA。
- 控制 SAHI 切片数量，避免过度切片导致显存和延迟失控。
- 避免重复模型加载和重复预处理。
- 谨慎处理高分辨率广角视频，例如 5210x1440。
- 在保证召回的前提下，优先减少无意义候选数量，降低选择层负担。

## Coding Expectations

- 先给出分层设计和模块职责，再写实现。
- 对关键类、关键函数、关键状态转移写中文解释。
- 如果用户要你实现功能，完整实现 selection 和 tracking，不要只给伪代码。
- 如果用户要你 review 代码，重点检查是否违反 Detection/Tracking 解耦、是否错误使用 confidence 主导选球、是否缺少预测与状态机。
- 如果用户要你调参，围绕 `match_distance`、`max_speed`、`max_acceleration`、`max_lost_frames`、SAHI slicing 做系统性分析。

## Preferred Data Flow

优先采用以下调用链：

```text
frame
  -> detector
  -> candidate filter
  -> selector
  -> tracker state update / prediction
  -> renderer
  -> exporter
```

如果现有代码不是这个顺序，重构时以职责边界和可验证性为先，不要做表面改名式重构。
