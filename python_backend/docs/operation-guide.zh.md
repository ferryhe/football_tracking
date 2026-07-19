# 足球追踪 P3 操作与验收指南

这份文档是本仓库的中文权威操作手册。所有路径和命令都以**仓库根目录**为起点；不要先进入 `python_backend/`，也不要手工设置 `PYTHONPATH`。

## 1. 开始前准备

- 根目录 `.venv` 已创建，并安装 `python_backend/requirements.txt`
- Node.js、pnpm 和根目录 `node_modules` 可用
- 原视频位于 `python_backend/data/`
- 配置位于 `python_backend/config/`
- 配置引用的检测权重存在；默认是 `python_backend/weights/football_ball_yolo.pt`

先运行统一检查：

```powershell
pnpm check
```

检查失败时先修复输出中的缺项。它必须返回成功，才能启动界面。

## 2. 唯一正式命令

```powershell
# 启动本地界面；打开命令最终打印的浏览器地址
pnpm start

# 查看或停止由本入口托管的进程
pnpm status
pnpm stop

# 后端、OpenAPI、广播页、类型检查和生产构建
pnpm test

# 训练候选球分类器；参数与 --help 由正式入口透传
pnpm train -- --help

# 验收一个已进入 ready 的整视频 P3 run
pnpm validate:full-video -- --run-dir python_backend/outputs/runs/<视频>/<run-id> --resume
```

入口会自行选择根 `.venv`、把 `python_backend` 放到导入路径最前面并固定工作目录。`python_backend/start_ui.cmd` 和底层 Python 脚本只保留兼容性，不是另一套正式操作方式。

`pnpm start` 会给后端最多 180 秒完成有界的持久状态恢复。在检测器或 review-proxy 工作后首次冷启动时，`Waiting for application startup` 可能持续一分钟以上；除非启动器达到有界的健康检查失败并指向 `backend.log`，不要中途终止。

## 3. 页面分工

- **广播成片 `/broadcast`**：P3 全场交付的推荐入口，包含设置、证据复核、重算、渲染和交付。
- **跑基线 `/baseline`**：旧的局部试跑或基线调参入口，可以限制帧范围，不代表完成 P3 交付。
- **AI 分析 `/ai`**：解释既有 run 并给出调参建议。
- **成品任务 `/deliverable`**：旧 follow-cam 与候选集锦流程，不替代 P3 的证据绑定广播成片。
- **历史 `/history`**：查看和管理历史任务。

## 4. 广播成片三步流程

### 第一步：设置并启动全场追踪

1. 选择原视频和配置。
2. 在三个**不同的真实帧**上完成确认；三帧分辨率必须一致。
3. 确认球场多边形至少三个点、面积非零且全部在画面内；排除区也必须合法。
4. 选择 1–30 的复核窗口上限。
5. 启动任务，并保存地址栏中的 `/broadcast?run=<run-id>`。

P3 只接受整视频：`start_frame=0`，`max_frames` 必须为空或覆盖完整源视频。切换视频或配置会使旧校准失效，必须重新确认。

常见状态顺序：

```text
setup → tracking → needs_review → recomputing → trajectory_ready → rendering → ready
```

页面刷新、复制 URL 到新窗口或返回任务时，会从服务器状态恢复当前父任务和活动子任务。

### 第二步：完整复核并重算

每个候选必须恰好选择一次：

- `confirm_ball`：确认是比赛用球
- `reject_noise`：确认是噪点；必须选择具体噪声子类型
- `mark_unknown`：证据不足，保留未知

非空队列必须填写复核人，且所有候选完成后才能提交。零候选不是自动跳过：必须明确点击“无需复核继续”。蒙太奇缺失、证据 URL 未通过校验或队列过期时不要猜测，刷新当前 run 后重新读取证据。

提交后页面会启动轨迹重算。若决策已经写入但子任务排队失败，使用“重试重算”；不要重复创建或手改 `review_decisions.json`。HTTP 409 表示证据已经变化，应刷新并从当前证据重新开始。

### 第三步：渲染与交付

轨迹进入 `trajectory_ready` 后再渲染。默认使用 1920×1080；允许范围是宽 320–7680、高 180–4320。页面显示 `ready` 以后，下载链接仍必须通过当前不可变 generation 的校验。

交付集合必须**恰好包含**以下 8 项：

1. `broadcast.mp4`
2. `broadcast_quality_report.json`
3. `camera_target.csv`
4. `ball_track.v2.csv`
5. `review_decisions.json`
6. `action_track.csv`
7. `candidate_classifications.jsonl`
8. `ball_candidates.jsonl`

存在 blocking reason、`metadata_conflict`、`missing_after_ready_commit`、缺少产物或状态 generation 不一致时，都不能称为完成。

## 5. 整视频媒体验收

`ready` 证明公开产物、哈希和血缘完整；它不等同于媒体和视觉验收。对 ready run 运行：

```powershell
pnpm validate:full-video -- --run-dir python_backend/outputs/runs/<视频>/<run-id> --resume
```

正式验收会：

- 重新验证 quality report 与最终 generation 的血缘
- 探测源视频和成片的视频/音频流与时长
- 分段完整解码成片，并检查首帧、中帧、尾帧和各段中心帧
- 即使复用了验收进度，也会在发布 `pass` 前用 FFmpeg 严格独立完整解码并复核帧数
- 原子写入 `broadcast_acceptance_report.v1.json`
- 用 `broadcast_acceptance_progress.v1.json` 记录可恢复的验收分段

`--resume` 只把工具版本、quality report、源视频、成片和抽样计划全部相同的已完成分段作为调度缓存；任一身份变化都会使旧进度失效。缓存不能绕过最终严格全片解码。这是**验收扫描**的断点续做，不表示初始追踪或渲染支持逐帧 checkpoint。

最终 FFmpeg 严格解码是独立终局门禁，不读取可写 checkpoint；因此每次 `--resume` 都会从头完整解码一次最终 1080p 成片，耗时大致等于一次全片解码。CLI 会把该阶段的开始/结束 JSON 写到 stderr；若此时中断，不会发布新的 `pass`，下次必须重跑这一终局门禁。

报告状态含义：

- `pass`：血缘、媒体、时长和分段检查通过
- `fail`：发现确定的契约或媒体错误
- `unavailable`：缺少可信探测器等原因，无法完成检查；同样不能交付

当前正式质量契约明确声明 `source_audio_not_preserved`。源视频有音频而成片无音频时，验收报告会记录 `known_limitation`，不会伪装成已保留音频；如果报告能力声明与实际流不一致，则验收失败。

最后还要人工抽查：复核蒙太奇是否对应真实球、球轨迹是否有明显大跳或假阳性、镜头是否频繁甩向边线/观众、开头/中段/结尾是否正常。只有机器报告和视觉检查都通过，才可称为可交付。

## 6. 恢复、取消和重启

- 保存 `/broadcast?run=<父或子 run-id>`；刷新后由服务器选择权威父任务和最新活动子任务。
- 活动任务可以在页面取消；重算和渲染取消的是对应子任务。
- 服务重启后，已经原子提交的重算/渲染 generation 会通过 operation report 对账；未安全提交的操作会失败或重新排队，不能手工改注册表冒充完成。
- **初始全场追踪没有逐帧服务重启续跑**。如果服务在该阶段中断，run 会标记失败，必须创建新任务。
- 不要直接修改或删除 ready generation；新结果必须产生新的不可变 generation。

## 7. 常见故障

| 现象 | 处理方式 |
| --- | --- |
| `pnpm check` 报 Python、pnpm、依赖或目录缺失 | 按输出补齐根 `.venv`、依赖、输入、配置或权重，再重新检查 |
| 端口被占用 | 启动器会选择可用端口并打印最终地址；不要自行复用未知进程 |
| 浏览器 `/api` 404、502 或后端 health 失败 | 先 `pnpm status`，再 `pnpm stop`、`pnpm check`、`pnpm start`；查看启动器打印的日志路径 |
| 旧进程无法停止 | 入口只会终止状态文件中根 PID 与创建身份仍匹配的受管进程；`status` 还会确认当前端口监听者仍属于该进程树，不会杀无关进程 |
| 校准提交失败 | 确认三个不同帧、合法球场/排除区、1–30 复核上限以及整视频范围 |
| 复核证据过期或返回 409 | 刷新当前 run，丢弃旧哈希和旧 action，再按当前证据复核 |
| 已提交复核但未开始重算 | 使用页面“重试重算”，不要重复写决策文件 |
| 重启后初始追踪失败 | 新建 P3 run；当前只支持重算/渲染的安全 generation 对账 |
| `ready` 但验收失败 | 以验收报告中的失败检查为准；不要仅凭文件存在或页面状态交付 |

## 8. 关闭与清理

```powershell
pnpm stop
pnpm status
```

重复停止是幂等操作。托管入口只清理自己记录且根 PID/创建身份仍匹配的进程；状态检查还要求监听者属于该根进程树，不会终止占用同一端口的无关程序。
