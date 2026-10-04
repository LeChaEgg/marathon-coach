# 东京马拉松 2027 · AI 跑步教练

直接和 agent（Claude Code / Cursor）对话即可，它会自动同步 Intervals.icu 数据、跑分析脚本，再给出教练判断。

| 你说 | 得到 |
|---|---|
| “早上好，今天练什么” | 恢复指标 vs 个人基线、负荷风险、本周计划中今天的课 → 今日训练处方 |
| “跑完了，分析一下”（可附 RPE/疼痛等感受） | 跑力评分（有氧/阈值/速度/耐力 + 综合，VDOT 刻度，已做温湿度修正）、分公里、心率区间、脱钩、有氧指数、跑姿、快段、最佳分段、同类对比 → 分析和明天的建议 |
| “打开训练中心” | 本地网页 `python3 -m coach serve --open`（http://127.0.0.1:8787）：今日 / 本周 / 跑步 / 赛季 / 能力五页。按钮直接同步数据、生成数据报告、提交反馈、调整激进程度；教练写完结论后页面自动刷新 |
| “周复盘” | 本周总结、12 周趋势、能力面板、全马预测、破3差距 → 下周逐日计划（写入 `plan/weeks/`） |

跑后反馈越完整，判断越准：实际距离/时间、RPE 1–10、疼痛 0–10 及位置（跑中/跑后/次日）、步态是否改变、异常疲劳、补给。

## 命令行（可选）

```bash
python3 -m coach today                 # 晨报
python3 -m coach run [ID|YYYY-MM-DD]   # 跑后分析，默认最近一次
python3 -m coach week [YYYY-MM-DD]     # 周复盘
python3 -m coach runs --days 60 --group long
python3 -m coach serve --open          # 训练中心（本地网页）
python3 -m coach dash                  # 静态看板 reports/dashboard.html
python3 -m coach log --activity i123 --rpe 6 --pain 1 --pain-loc 左胫骨内侧 --note "..."
python3 -m coach sync [--full]         # 首次全量约 7 分钟，之后增量几秒
```

所有报告命令加 `--offline` 可以只用本地缓存。

## 文件

- `athlete.toml`：心率区间、目标、PB、伤病史。阈值心率等变化时改这里。
- `plan/season_plan.md`：23 周周期计划；`plan/weeks/`：每周计划。
- `reports/`：每日/每次跑步/每周报告，末尾附教练结论。
- `journal/feedback.jsonl`：主观反馈记录。
- `data/`：Intervals.icu 本地缓存（可以删除后用 `sync --full` 重建）；`data/weather/` 是 Open-Meteo 逐小时天气缓存。
- `coach/server.py` + `coach/templates/app.html`：训练中心；`reports/dashboard.html`：离线静态看板。
- `docs/methodology.md`：各指标的定义与解读口径。
