# 本地 FIT 数据源参考

本文件是 `scripts/fit_reader.py` 的命令手册与字段语义说明。复盘方法见 `review-methodology.md`，隐私边界见 `privacy-safety.md`。

## 环境准备

依赖：Python 3.10+（`garmin-fit-sdk` 当前要求）+ `garmin-fit-sdk`（纯 Python，无编译步骤）。

按项目惯例，每台机器在 skill 目录内建独立 venv（`.gitignore` 已排除，不随仓库分发）：

```text
Windows:  python -m venv .venv-windows && .venv-windows\Scripts\python -m pip install -r scripts\requirements.txt
macOS:    python3 -m venv .venv-macos && .venv-macos/bin/python -m pip install -r scripts/requirements.txt
Linux:    python3 -m venv .venv-linux && .venv-linux/bin/python -m pip install -r scripts/requirements.txt
```

venv 跨平台不兼容：macOS 建的 venv 在 Windows 上不可用，反之亦然；每台机器自建即可，不影响其他机器。

验证环境：

```text
.venv-windows\Scripts\python scripts\fit_reader.py --help
```

报 `缺少 garmin-fit-sdk` 时按脚本提示重装依赖，不要手写解析器绕过。

## FIT 目录配置

优先级：`--dir <路径>` > 环境变量 `FIT_WORKOUT_REVIEW_FIT_DIR` > 默认 `~/FIT`。

- 不要把具体机器的绝对路径写进任何提交；示例一律用占位符。
- 目录由用户的同步工具（keep、Garmin Connect IQ、第三方脚本等）维护；本 Skill 不管理目录内容。

## 文件名约定

用户目录中的文件名格式（由同步脚本生成，`fit_reader.py` 的 `list` 依赖它做零成本筛选）：

```text
YYYYMMDD-HHMMSS-运动类型[-描述]-活动ID.fit
例：20261005-173241-running-青岛市-35min3×(20s@436-425_60s)-647281649.fit
```

- 日期/时间部分用于 `--days` 筛选；
- 运动类型段（`running` / `trail_running` / `cycling` / `walking` / `strength_training` ...）用于 `--sport` 筛选；
- 描述段（城市、训练结构摘要）只用于向用户呈现候选，不用于技术判断；
- 文件名不符合约定时，`list` 退回打开文件读 `start_time` 筛选（较慢，仍可用）。

## 命令与数据梯度

| 梯度 | 命令 | 输出 |
|---|---|---|
| 1 记录摘要 | `list [--days N] [--sport running] [--limit N]` | 文件名、日期、运动类型、大小；不打开文件 |
| 2 活动详情 | `summary --file <文件名或唯一子串>` | session 指标、workout_steps、quality、derived |
| 3 分圈/分段 | `laps --file ... [--max-laps 40]` | 每圈的时长、距离、配速、心率、功率、爬升、步频 |
| 4 自定义时间窗 | `window --file ... --last-minutes N [--sample N]` 或 `--from-minutes A --to-minutes B` | 采样后的秒级记录（时间偏移、心率、步频、功率、速度、海拔、温度） |

`--file` 接受完整文件名、不含扩展名的文件名、或文件名的唯一子串（如活动 ID `647281649`）。

### summary 字段语义

`session`（来自 FIT `session` 记录，设备已聚合）：

- `sport`：运动类型（中文标签，如“跑步”“越野跑”）；
- `start_time`：本地开始时间（按文件内时区偏移换算；无偏移时按 UTC+8 并在置信度中说明）；
- `total_timer_time_s` / `total_distance_m`：运动时间与距离；
- `avg_heart_rate` / `max_heart_rate`：心率；
- `avg_power` / `max_power` / `normalized_power`：功率（仅带功率传感器时存在）；
- `avg_speed_mps` / `max_speed_mps`：速度（m/s）；
- `total_ascent_m` / `total_descent_m`：累计爬升/下降；
- `avg_vertical_oscillation`（mm）/ `avg_stance_time`（ms）/ `avg_step_length`（mm）：跑步动力学（带跑步动力学传感器时存在）；
- `total_calories`：设备估算消耗。

`workout_steps`（来自 FIT `workout_step` 记录，训练执行计划）：

- `intensity`: `active` / `rest`；`rest` 段即组间恢复段；
- `target_type`: `speed` / `heart_rate` / `open` / 其他；
- `target_pace_s_per_km`（速度段）：`[最快端, 最慢端]`，秒/公里；
- `target_heart_rate`（心率段）：`[下限, 上限]`；
- `duration_time_s`：该段目标时长；
- `repeat_steps`：重复次数；
- `duration_type: repeat_until_steps_cmplt` 表示“重复到步骤完成”的循环头。

间歇结构判断顺序：先看 `workout_steps`（第一证据），再用 `laps` 实际值对照。两者矛盾时分别陈述，不自动裁决。

`quality`（数据完整性，必须如实反映到置信度）：

- `heart_rate` / `cadence` / `power` / `speed` / `altitude`：`ok`（覆盖率 ≥ 95%）/ `partial` / `missing` + `coverage` 数值；
- `env.temperature`：`mean_c` / `min_c` / `max_c` 与状态；温度明显越界（<0 或 >45）的记录不计入有效值；
- `env.humidity`：本地 FIT 一般没有湿度字段，固定为 `missing`。

`derived`（可复算）：

- `avg_cadence`：全记录平均步频；
- `hr_drift_last_q_minus_first_q`：末 1/4 平均心率 − 首 1/4 平均心率（粗略漂移，仅输出稳定时参考）；
- `pace_s_per_km_avg`：由平均速度换算的秒/公里。

### laps 字段语义

每圈：`timer_time_s`（运动时间）、`distance_m`、`pace_s_per_km`、`avg_heart_rate`、`max_heart_rate`、`avg_power`、`ascent_m`、`descent_m`、`avg_cadence`、`total_calories`。超过 `--max-laps` 时只保留首尾各一半并加省略标记——完整分圈表只在用户要求时展开。

### window 字段语义

`records[]` 每点：`t_offset_s`（距活动开始秒数）、`hr`、`cad`、`pow`、`spd_mps`、`pace_s_per_km`、`alt_m`、`temp_c`。`--sample` 控制输出点数，默认 60；时间窗之外的点不读取、不输出。

## 隐私裁剪位置

`fit_reader.py` 在输出前已裁剪：不输出经纬度、`semicircle` 原始值、course/位置 record、设备序列号、人体资料字段。输出中的 `file` 字段是用户自己的文件名（用户已知信息），不是泄露。

不要绕过脚本直接调用 `garmin_fit_sdk` 解析 record 流；如需新增字段，改 `scripts/fit_reader.py` 并保持上述裁剪边界。

## 与 COROS 云数据源的差异

| 维度 | 本 Skill（本地 FIT） | coros-workout-review（COROS MCP） |
|---|---|---|
| 账号 / OAuth | 无 | 需要 COROS 账号授权 |
| 数据位置 | 用户本地目录 | COROS 云端 |
| 结构化训练段 | FIT `workout_step` | 云端训练计划字段 |
| 训练效果 / 恢复模型 | 无（FIT 不含） | 设备模型输出（低证据优先级） |
| 湿度 | 无（标 missing） | 视设备而定 |
| 位置字段 | 读取器已裁剪，不输出 | 云端接口按隐私边界不请求 |

复盘方法（`review-methodology.md`）对两者相同；数据源差异只影响证据优先级第 6/7 条是否可用。

## 退出码

- `0` 成功；
- `2` 目录或参数错误（目录不存在、无 .fit 文件）；
- `3` 文件解析失败；
- `4` 找不到目标文件。

非 0 退出时按 SKILL.md「失败时如何结束」处理，不编造数据继续。
