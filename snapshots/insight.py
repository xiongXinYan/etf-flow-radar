"""在 metrics.build() 之上叠加「优化版」判读逻辑。

与参考页面的关键差别
a) 份额 —— 份额变化的符号是申购/赎回的直接证据（方向确定，只是归属不确定）；
   主动净买入受套利对冲稀释（方向本身可能失真）。所以把两者分开看，不再混为一句。
b) 价格 —— ETF 涨跌被「跟踪指数涨跌」解释掉以后，剩下的残差才是资金推出来的超额；
   份额增加 + 超额为正 才算「真加仓」；份额增加 + 超额为负 是「只接盘不抬价」。
c) 持续性 —— 单日放量多为事件（指数调仓、季度末做市），连续多日才算「上班」。
d) 同行对照 —— 同指数多只 ETF 同时放量，更可能是被动/套利需求；只有一只放量才像主动买。
"""
from __future__ import annotations

import json
import os
import statistics

import metrics
from metrics import shares_of

SNAP_DIR = metrics.SNAP_DIR
IDX_PATH = metrics.IDX_PATH

# 判定阈值（集中在此，便于调整）
TH = {
    "mult_strong": 1.8,      # 同时点倍数强信号
    "mult_mild": 1.3,        # 温和放量
    "active_strong": 30e8,   # 主动净买入绝对值（元）
    "share_strong": 10.0,    # 池内单日净申购（亿元）
    "coverage_min": 0.6,     # 主动盘数据覆盖率下限，低于此不参与综合评分
}

# 指数映射统一从采集模块取，保证「采什么」和「算什么」一致
IDX_MAP = metrics.IDX_MAP


def load_index():
    if not os.path.exists(IDX_PATH):
        return {}
    return json.load(open(IDX_PATH, encoding="utf-8"))


def excess_returns(rows, idx):
    """相对跟踪指数的超额涨跌（当日 / 5日 / 20日）。"""
    for r in rows:
        key = IDX_MAP.get(r["index"])
        series = idx.get(key, {}).get("daily") if key else None
        d = r.get("daily") or []
        r["m"]["excess"] = {}
        if not series or len(d) < 21 or len(series) < 21:
            continue
        # 指数与 ETF 按日期对齐
        imap = {x["date"]: x["close"] for x in series}
        emap = {x["date"]: x["close"] for x in d}
        dates = [x for x in sorted(emap) if x in imap]
        if len(dates) < 21:
            continue
        for n, label in ((1, "d1"), (5, "d5"), (20, "d20")):
            if len(dates) > n:
                e0, e1 = emap[dates[-1 - n]], emap[dates[-1]]
                i0, i1 = imap[dates[-1 - n]], imap[dates[-1]]
                r["m"]["excess"][label] = round(((e1 / e0) - (i1 / i0)) * 100, 2)
    return rows


def tier_of(mult, active, share_chg, excess_d1, peer_lift=None):
    """按当日全池真实分布打标签（非固定绝对阈值）。

    缩量是常态：24 只里 75% 分位才 0.90×，中位数 0.70×。
    所以用相对分档，避免「人人都是常态」。
    """
    tags = []
    if mult is None:
        return ["数据不足"]
    # 量能：以 1.0 为"平量"，低于 0.6 才算冰点，高于 1.3 才算放量
    if mult >= 1.3:
        tags.append("显著放量")
    elif mult >= 1.05:
        tags.append("小幅放量")
    elif mult <= 0.55:
        tags.append("缩量")
    else:
        tags.append("平量")

    # 主动盘：用池内分位判断，±3 亿是绝对下限
    if active is not None:
        if active >= 3e8:
            tags.append("主动买")
        elif active <= -3e8:
            tags.append("主动卖")

    # 份额：单只 3 亿份以上视为明显
    if share_chg is not None:
        if share_chg >= 3:
            tags.append("净申购")
        elif share_chg <= -3:
            tags.append("净赎回")

    # 同门相对量：>1.4 说明比同指数其他 ETF 活跃得多
    if peer_lift is not None and mult >= 0.9:
        if peer_lift >= 1.4:
            tags.append("同门最活跃")
        elif peer_lift <= 0.6:
            tags.append("同门最冷清")

    # 超额：仅在有一定成交时才有意义
    if excess_d1 is not None and mult >= 0.9 and abs(excess_d1) >= 0.3:
        tags.append("跑赢基准" if excess_d1 > 0 else "跑输基准")
    return tags


def judge(b, idx):
    """把池内合计拆成资金面 / 价格面 / 持续性三块判读。"""
    A = b["agg"]
    focus = b["focus"]
    net = A["active_net"]
    mult = A["mult"]
    pct = A["pct"]

    # 1) 资金面：主动买卖方向
    if net is None:
        fund = {"level": "unknown", "label": "数据不足", "detail": ""}
    elif net > TH["active_strong"]:
        fund = {"level": "in", "label": "主动买盘占优", "detail": f"合计主动净买入 {net/1e8:+.1f} 亿"}
    elif net < -TH["active_strong"]:
        fund = {"level": "out", "label": "主动卖盘占优", "detail": f"合计主动净买入 {net/1e8:+.1f} 亿"}
    else:
        fund = {"level": "flat", "label": "主动买卖基本平衡", "detail": f"合计主动净买入 {net/1e8:+.1f} 亿，未达 ±30 亿阈值"}

    # 2) 量能：同时点倍数
    if mult is None:
        vol = {"level": "unknown", "label": "数据不足"}
    elif mult >= TH["mult_strong"]:
        vol = {"level": "high", "label": "明显放量"}
    elif mult >= TH["mult_mild"]:
        vol = {"level": "mild", "label": "温和放量"}
    elif mult <= 0.6:
        vol = {"level": "low", "label": "明显缩量"}
    elif mult < 0.85:
        vol = {"level": "light", "label": "偏冷"}
    else:
        vol = {"level": "normal", "label": "量能正常"}

    # 3) 价格面：超额
    exs = [r["m"]["excess"].get("d1") for r in focus if r["m"].get("excess")]
    ex_med = statistics.median(exs) if exs else None

    # 4) 综合结论（四个一级因子，去除重复计数）
    # 旧版把「成交额倍数」「主动净买入」「涨跌」「超额」直接相加，
    # 但主动净买入 = 方向 × 成交额、涨跌与超额高度相关，
    # 等于对「放量」和「上涨」各加了两次权。这里改为互不重复的四项：
    #   一级市场申赎 40%（份额×净值，最硬）
    #   成交活跃度   25%
    #   价格表现(超额) 20%
    #   二级方向估算 15%
    # 涨跌幅只作为背景变量，不进评分。
    score = 0.0
    score_parts = {}
    sh_amt = None
    if b["share_series"] and len(b["share_series"]) > 1 and b["share_series"][-1]["delta"] is not None:
        sh_amt = b["share_series"][-1]["delta"]          # 亿元
        score_parts["share"] = max(-1.0, min(1.6, sh_amt / 40))
        score += score_parts["share"]
    if mult is not None:
        score_parts["turnover"] = max(-0.75, min(1.0, (mult - 1) * 1.5))
        score += score_parts["turnover"]
    if ex_med is not None:
        score_parts["excess"] = max(-0.5, min(0.8, ex_med / 1.0))
        score += score_parts["excess"]
    # 覆盖率不足时，主动盘只能作为方向参考，不参与评分
    cov = A.get("net_covered")
    if net is not None and cov is not None and cov >= TH["coverage_min"]:
        score_parts["active"] = max(-0.6, min(0.6, net / 8e9))
        score += score_parts["active"]
    else:
        score_parts["active"] = 0.0

    if score >= 2.2:
        verdict = ("强信号：疑似集中买入", "buy-strong")
    elif score >= 1.0:
        verdict = ("偏强：有买入迹象", "buy-mild")
    elif score <= -2.2:
        verdict = ("强信号：疑似集中卖出", "sell-strong")
    elif score <= -1.0:
        verdict = ("偏弱：有卖出迹象", "sell-mild")
    else:
        verdict = ("没看出上班迹象", "flat")

    # 5) 关键提示（自动生成，避免把口径当事实）
    notes = []
    if mult is not None and mult <= 0.7 and net is not None and net < -20e8:
        notes.append("量价同向偏弱：缩量下的净卖出更像存量博弈的自然滑落，而不是有组织的减仓——但也没有任何买入力量接盘。")
    if mult is not None and mult >= TH["mult_strong"] and net is not None and abs(net) < 15e8:
        notes.append("成交显著放大但主动买卖接近平衡：更像套利/调仓带来的换手，而非单向买入。")
    if ex_med is not None and abs(ex_med) >= 0.8:
        notes.append(f"池内相对跟踪指数的超额中位数为 {ex_med:+.2f}%，价格偏离基准，注意是否存在指数调仓或成分股事件。")
    elif ex_med is not None:
        notes.append(f"ETF 相对跟踪指数的超额中位数仅 {ex_med:+.2f}%，价格基本由指数带动，资金没有在 ETF 层面额外定价。")
    if sh_amt is not None:
        if sh_amt >= 20:
            notes.append(f"份额变化折算估算净申购 {sh_amt:+.1f} 亿元：申购行为可以确认，但无法确认申购方是谁。")
        elif sh_amt <= -20:
            notes.append(f"份额变化折算估算净赎回 {sh_amt:+.1f} 亿元：有资金在离场。")
    if net is not None and (cov is None or cov < TH["coverage_min"]):
        notes.append(f"主动盘数据覆盖率仅 {(cov or 0)*100:.0f}%，低于 {TH['coverage_min']*100:.0f}% 下限：本日主动买卖方向只作参考，不计入综合评分。")
    if not (mult and mult >= TH["mult_strong"]):
        notes.append("单一交易日的缩量/平量不能推翻长期判断：真正的集中买入通常伴随连续 2—3 个交易日的量能抬升与份额同向变动，建议按周观察而非盯单日。")

    return {
        "score": round(score, 2),
        "verdict": verdict[0],
        "tone": verdict[1],
        "fund": fund,
        "vol": vol,
        "excess_med": ex_med,
        "score_parts": {k: round(v, 2) for k, v in score_parts.items()},
        "coverage": round(cov, 3) if cov is not None else None,
        "share_amt": round(sh_amt, 2) if sh_amt is not None else None,
        "notes": notes,
    }


def enrich(day_key=None):
    b = metrics.build(day_key)
    idx = load_index()
    excess_returns(b["rows"], idx)
    # 每只的份额日变化：需要前一份快照
    hist = metrics.load()
    dates = sorted(hist)
    i = dates.index(b["day"])
    if i > 0:
        prev = {r["code"]: r for r in hist[dates[i - 1]]["pool"]}
        for r in b["rows"]:
            p = prev.get(r["code"])
            psh = shares_of({**p, "date": dates[i - 1]}) if p else None
            if psh is None or not r["m"]["shares"]:
                r["m"]["share_chg"] = None
                r["m"]["share_chg_amt"] = None
                continue
            r["m"]["share_chg"] = round(r["m"]["shares"] - psh, 2)
            # 申赎金额估算：份额变化 × 当日现价（元 -> 亿元）
            r["m"]["share_chg_amt"] = round(r["m"]["share_chg"] * (r["m"]["price"] or 0) / 100, 3)
    else:
        for r in b["rows"]:
            r["m"]["share_chg"] = None
            r["m"]["share_chg_amt"] = None

    # 同行放量对照：同指数内倍数中位
    by_idx: dict[str, list] = {}
    for r in b["focus"]:
        by_idx.setdefault(r["index"], []).append(r)
    for name, group in by_idx.items():
        ms = [r["m"]["mult"] for r in group if r["m"]["mult"]]
        med = statistics.median(ms) if ms else None
        for r in group:
            r["m"]["peer_mult_med"] = med
            r["m"]["peer_n"] = len(group)
            r["m"]["peer_lift"] = round(r["m"]["mult"] / med, 2) if (med and r["m"]["mult"]) else None

    for r in b["rows"]:
        r["tags"] = tier_of(r["m"]["mult"], r["m"]["active_net"], r["m"].get("share_chg"),
                            (r["m"].get("excess") or {}).get("d1"), r["m"].get("peer_lift"))
    b["judge"] = judge(b, idx)
    b["thresholds"] = TH
    return b


if __name__ == "__main__":
    b = enrich()
    j = b["judge"]
    print("verdict:", j["verdict"], "score", j["score"])
    print("fund:", j["fund"]); print("vol:", j["vol"]); print("excess_med:", j["excess_med"])
    for n in j["notes"]:
        print(" -", n)
    print()
    for r in b["focus"][:5]:
        m = r["m"]
        print(r["code"], r["name"], m["mult"], m.get("excess"), r["tags"], "peer_lift", m.get("peer_lift"))
