# AI 跑步教练（Marathon Coach）

把 [Intervals.icu](https://intervals.icu) 里的训练和恢复数据同步到本地，用脚本算出固定格式的报告（跑力评分、负荷、恢复基线、全马预测、目标差距），再让 AI agent（Claude Code / Cursor 等）读报告、给出教练判断和周计划。纯 Python 标准库，没有第三方依赖；数据都在你自己的机器和仓库里。

> *English in short:* a zero-dependency Python toolkit that syncs your Intervals.icu data, computes readiness / fitness reports (VDOT-scale run scoring, load, recovery baselines, marathon prediction, gap to goal) and serves a local dashboard. An AI agent (Claude Code, Cursor, …) reads `AGENTS.md` + the reports to act as your coach. Docs are in Chinese; all athlete-specific settings live in `athlete.toml`.

作者自己的数据也留在这个仓库里（备战 2027 东京马拉松，目标破 3），当作一份完整的使用示例；你要用自己的数据，看下面「上手」。

> ⚠️ 这是训练辅助工具，不是医疗建议。出现骨性压痛、跳跃痛、跛行或疼痛持续到次日，请停跑并咨询运动医学医生。

## 你说什么，得到什么

| 你说 | 得到 |
|---|---|
| “早上好，今天练什么” | 恢复指标 vs 个人基线、负荷风险、本周计划中今天的课 → 今日训练处方 |
| “跑完了，分析一下”（可附 RPE/疼痛等感受） | 跑力评分（有氧/阈值/速度/耐力 + 综合，VDOT 刻度，已做温湿度修正）、分公里、心率区间、脱钩、有氧指数、跑姿、快段、最佳分段、同类对比 → 分析和明天的建议 |
| “打开训练中心” | 本地网页 `python3 -m coach serve --open`（http://127.0.0.1:8787）：今日 / 本周 / 跑步 / 赛季 / 能力五页。按钮直接同步数据、生成数据报告、提交反馈、调整激进程度；教练写完结论后页面自动刷新 |
| “周复盘” | 本周总结、12 周趋势、能力面板、全马预测、目标差距 → 下周逐日计划（写入 `plan/weeks/`） |

跑后反馈越完整，判断越准：实际距离/时间、RPE 1–10、疼痛 0–10 及位置（跑中/跑后/次日）、步态是否改变、异常疲劳、补给。

## 上手

### 0. 前置条件

- **Python 3.11 或更新**（用到标准库 `tomllib`）：`python3 --version`
- 一个 [Intervals.icu](https://intervals.icu) 账号，并且你的手表/平台（Garmin、Apple Watch、Strava 等）已经同步到里面
- 一个能读本仓库的 AI agent：Claude Code 或 Cursor。它读 `AGENTS.md`（项目规则）和 `.cursor/skills/`（三个流程的步骤）。不用 agent 也能用，命令行和网页照样可用，只是没有教练结论。

不需要 `pip install` 任何东西。

### 1. 拿到代码并初始化

```bash
git clone <本仓库地址> marathon-coach
cd marathon-coach
python3 -m coach init --reset --yes    # 清掉原作者的个人数据（报告/计划/反馈/背景），生成配置模板
```

`--reset` 只在你要用自己的数据时加；它会删除 `reports/`、`plan/`、`journal/feedback.jsonl`、`docs/athlete_background.md`、`docs/running_pb.md`，并用 `athlete.example.toml` 覆盖 `athlete.toml`。想先看看会删什么，去掉 `--yes` 再跑一次。

### 2. 配置你自己的档案：`athlete.toml`

打开 `athlete.toml`，把带 “← 必改” 的项改掉：

- `[race]`：比赛名、日期、目标成绩（秒）、目标简称 `goal_label`
- `[hr]`：LTHR、最大心率、各区间上限（和 Intervals.icu 里的心率设置保持一致）
- `[injury]`：伤病史和重点防范部位
- `[pb]`：5 km / 10 km / 半马 PB（没有就删）
- `[devices] baseline_since`：换过手表的话，填换设备的日期，之前的 HRV/静息心率不参与基线
- `[location]`：天气修正用的默认位置，建议只留一位小数
- `[score]`：评分用的心率范围，注释里给了按 LTHR 比例推算的办法

不知道 LTHR 和最大心率时，先用 Intervals.icu 的估计值，之后做一次 30 分钟全力测试再更新（测试周怎么做见 `.cursor/skills/weekly-review/SKILL.md`）。

### 3. 填 Intervals.icu 凭据

1. Intervals.icu → **Settings → Developer Settings → API Key**，生成 key
2. 你的 athlete id 在设置页的网址或 API 页面里，形如 `i123456`
3. 编辑 `intervals_icu_api.txt`（`init` 已经从模板复制好）：

   ```text
   id: i123456
   token: 你的 API key
   ```

   或者不落盘，设置环境变量 `INTERVALS_ICU_ID` 和 `INTERVALS_ICU_TOKEN`。

`intervals_icu_api.txt` 已经在 `.gitignore` 里，不会被提交。**不要把 token 贴进 issue、日志或聊天。**

### 4. 同步数据

```bash
python3 -m coach sync --full      # 首次全量；2 年历史约 7 分钟。之后增量，几秒
```

数据缓存在 `data/`（已 gitignore，可随时删掉重建）。同步是只读的，脚本不会改你的 Intervals.icu。

### 5. 开始用

对 agent 说“早上好，今天练什么”，或者直接：

```bash
python3 -m coach today                 # 晨报
python3 -m coach run [ID|YYYY-MM-DD]   # 跑后分析，默认最近一次
python3 -m coach week [YYYY-MM-DD]     # 周复盘
python3 -m coach runs --days 60 --group long
python3 -m coach serve --open          # 训练中心（本地网页）
python3 -m coach dash                  # 静态看板 reports/dashboard.html
python3 -m coach log --activity i123 --rpe 6 --pain 1 --pain-loc 左小腿内侧 --note "..."
python3 -m coach sync [--full] [--rebuild]
```

所有报告命令加 `--offline` 可以只用本地缓存。第一次可以先让 agent 做一份赛季计划写到 `plan/season_plan.md`（参考 `docs/methodology.md`），周复盘再把每周计划写到 `plan/weeks/`。

## 多台设备使用

仓库里提交的是代码、配置、计划、报告和反馈；数据缓存和凭据不提交。换一台设备：

```bash
git clone <你的仓库地址> && cd <目录>
# 填凭据（intervals_icu_api.txt 或环境变量）
python3 -m coach sync --full
```

两台设备之间靠 git 同步 `reports/`、`plan/`、`journal/feedback.jsonl`。习惯：用之前 `git pull --rebase`，用完 `git add -A && git commit && git push`。`journal/feedback.jsonl` 只追加，已设置 `merge=union`，两台设备都写也不会冲突。

## 隐私

- 提交到 git 的包括：`athlete.toml`（年龄、体重、心率、伤病史）、训练报告和主观反馈。仓库公开的话这些都是公开的，介意就用私有仓库，或者提交前检查。
- `athlete.toml` 里的家庭坐标建议只留一位小数。`data/`（含各活动的 GPS 起点）和凭据不会被提交。
- 报告里的活动 ID（`i123456789`）是 Intervals.icu 的活动编号，没有 token 看不到内容。

## 文件

```text
athlete.toml            运动员档案：心率区间、目标、PB、伤病史（模板 athlete.example.toml）
coach/                  同步与分析脚本（纯标准库）；scoring.py 跑力评分，weather.py 天气，server.py + templates/app.html 训练中心
data/                   Intervals.icu 本地缓存（不提交，可重建）；data/weather/ 是 Open-Meteo 逐小时天气缓存
journal/feedback.jsonl  主观反馈与跑步类型修正（只追加）
plan/                   season_plan.md 赛季周期计划；weeks/ 每周逐日计划
reports/                daily / runs / weekly 报告 + 教练结论
docs/methodology.md     各指标的定义、阈值和模型局限
AGENTS.md               给 agent 的项目规则（角色、红绿灯、数据边界）
.cursor/skills/         三个核心流程的步骤（晨报、跑后分析、周复盘）
```

## 局限

- 跑力评分、目标准备度和全马预测都是基于心率储备和公开经验模型的估计，不是预测，更不是成功率；口径和局限见 `docs/methodology.md`。
- 周复盘里的“目标参考标准”表格默认是破 3（2:59:59）量级的数字，其他目标请在 `athlete.toml` 的 `[benchmarks]` 里改。
- 设备识别（Apple Watch / Garmin 等）靠活动里的设备名称，只用于同设备对比；wellness 基线按 `baseline_since` 切。

## 许可

[MIT](LICENSE)
