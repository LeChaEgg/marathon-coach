"""Plan aggressiveness presets (athlete.toml [plan].aggressiveness, 1–5).

Only load-based rules move with the level. Pain / red-flag rules (AGENTS.md traffic light) are fixed at every level.
"""

LEVELS = {
    1: {"name": "保守", "ramp": 0.05, "spike": 1.20, "acwr_yellow": 1.30, "acwr_caution": 1.15, "build_weeks": 2,
        "deload": (0.65, 0.75), "long_pct": 0.28, "long_step": 2, "quality": 1,
        "plan_policy": "min", "yellow_cut": "减量 30–50%，取消质量课"},
    2: {"name": "稳健", "ramp": 0.08, "spike": 1.25, "acwr_yellow": 1.40, "acwr_caution": 1.20, "build_weeks": 3,
        "deload": (0.70, 0.80), "long_pct": 0.30, "long_step": 2, "quality": 2,
        "plan_policy": "min", "yellow_cut": "减量 20–40%，取消质量课"},
    3: {"name": "标准", "ramp": 0.10, "spike": 1.30, "acwr_yellow": 1.50, "acwr_caution": 1.30, "build_weeks": 3,
        "deload": (0.70, 0.80), "long_pct": 0.30, "long_step": 3, "quality": 2,
        "plan_policy": "min", "yellow_cut": "减量 20–40%，取消质量课"},
    4: {"name": "积极", "ramp": 0.15, "spike": 1.50, "acwr_yellow": 1.70, "acwr_caution": 1.45, "build_weeks": 3,
        "deload": (0.75, 0.85), "long_pct": 0.33, "long_step": 3, "quality": 2,
        "plan_policy": "plan", "yellow_cut": "取消质量课，跑量减 0–20%"},
    5: {"name": "激进", "ramp": 0.20, "spike": 1.80, "acwr_yellow": 2.00, "acwr_caution": 1.60, "build_weeks": 4,
        "deload": (0.80, 0.90), "long_pct": 0.35, "long_step": 4, "quality": 3,
        "plan_policy": "max", "yellow_cut": "质量课缩短保留，跑量不减"},
}

POLICY = {
    "min": "规则区间与赛季计划取更保守者",
    "plan": "以赛季计划为准，不超过规则区间上限",
    "max": "规则区间与赛季计划取更积极者",
}


def level(cfg):
    lv = int((cfg.get("plan") or {}).get("aggressiveness", 3))
    return max(1, min(5, lv))


def style(cfg):
    lv = level(cfg)
    return lv, LEVELS[lv]


def summary(p):
    """Human-readable parameter list, in the order shown to the athlete."""
    return [
        ("周加量上限", f"{p['ramp'] * 100:.0f}%"),
        ("7天/此前周均 黄灯", f">{p['acwr_yellow']:.2f}（提示 >{p['acwr_caution']:.2f}）"),
        ("突增后冻结", f"本周 >{p['spike']:.2f}× 前4周均值时下周不加量"),
        ("减量周", f"连续加量 {p['build_weeks']} 周后，降到 {p['deload'][0] * 100:.0f}–{p['deload'][1] * 100:.0f}%"),
        ("长距离上限", f"≤ 周跑量 {p['long_pct'] * 100:.0f}%，且 ≤ 近4周最长 +{p['long_step']} km"),
        ("每周质量课", f"≤{p['quality']} 次"),
        ("与赛季计划", POLICY[p["plan_policy"]]),
        ("客观黄灯时", p["yellow_cut"]),
    ]
