"""核心指标计算：所有页面数字都由本模块从 snapshots/history.json 算出。

口径（与页面脚注一一对应）
1. 同时点放大倍数 = 当前累计成交额 / (20日均额 × 该标的此刻历史平均已走完比例)
   —— 解决「上午的半天成交额不能直接除全天均额」的问题。
2. 主动净买入 = (外盘 − 内盘) / (外盘 + 内盘) × 成交额   仅逐笔方向推算。
3. 份额 = 行情规模 ÷ 现价（推算值，非基金公司披露），日变化再乘现价得申赎市值估算。
4. 加仓指数：以区间首日为 100，「100 × 当日池内总份额 ÷ 首日总份额」。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_DIR = os.path.join(ROOT, "snapshots")
HIST_PATH = os.path.join(SNAP_DIR, "history.json")

# 交易时段（分钟），用于同时点对齐
SESSIONS = [(9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60)]


def minutes_elapsed(hhmm: str) -> int:
    h, m = int(hhmm[:2]), int(hhmm[2:4])
    cur = h * 60 + m
    done = 0
    for s, e in SESSIONS:
        if cur >= e:
            done += e - s
        elif cur > s:
            done += cur - s
    return done


def total_minutes() -> int:
    return sum(e - s for s, e in SESSIONS)


def load() -> dict:
    with open(HIST_PATH, encoding="utf-8") as f:
        return json.load(f)


def _daily_avg_amount(rec) -> float | None:
    """20日均额：新浪日K成交量(手)×100×当日均价(近似用收盘价)。排除今日。"""
    days = [d for d in rec.get("daily", []) if d["date"] < rec.get("date", "9999")]
    days = days[-20:]
    if len(days) < 5:
        return None
    vals = [d["volume_lots"] * 100 * d["close"] for d in days]
    return sum(vals) / len(vals)


def intraday_curve(rec) -> dict:
    """从近 N 日 5 分钟K 计算：每个时点累计成交额占全天的平均比例。"""
    m5 = rec.get("m5") or []
    by_day: dict[str, list] = {}
    for bar in m5:
        by_day.setdefault(bar["t"][:8], []).append(bar)
    curves = []
    for day, bars in by_day.items():
        bars.sort(key=lambda b: b["t"])
        total = sum(b["volume_lots"] for b in bars)
        if total <= 0 or len(bars) < 40:
            continue
        cum, pts = 0.0, {}
        for b in bars:
            cum += b["volume_lots"]
            pts[b["t"][8:12]] = cum / total
        curves.append(pts)
    # 48 个时点均值
    keys = sorted({k for c in curves for k in c})
    avg = {}
    for k in keys:
        vals = [c[k] for c in curves if k in c]
        if vals:
            avg[k] = sum(vals) / len(vals)
    return avg


def progress_at(curve: dict, hhmm: str) -> float:
    """按分钟插值取该时点历史平均已走完比例。"""
    if not curve:
        return 0.0
    keys = sorted(curve)
    target = hhmm
    prev = None
    for k in keys:
        if k >= target:
            if prev is None:
                return curve[k]
            # 线性插值
            t0, t1 = minutes_elapsed(prev), minutes_elapsed(k)
            v0, v1 = curve[prev], curve[k]
            t = minutes_elapsed(target)
            if t1 == t0:
                return v1
            return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
        prev = k
    return curve[keys[-1]]


def compute_row(rec, curve) -> dict:
    """单只 ETF 的当日指标。"""
    amt = rec.get("amount_yuan") or 0.0
    avg20 = _daily_avg_amount(rec)
    hhmm = (rec.get("time") or "150000")[8:12]
    # 收盘后用全天均额；盘中用同时点对齐
    if hhmm >= "1500":
        base = avg20
        ratio_mode = "close"
    else:
        prog = progress_at(curve, hhmm) or 1.0
        base = avg20 * prog if avg20 else None
        ratio_mode = "intraday"
    mult = round(amt / base, 2) if base else None

    outer, inner = rec.get("outer"), rec.get("inner")
    if outer and inner and (outer + inner) > 0:
        active_net = (outer - inner) / (outer + inner) * amt
    else:
        active_net = None

    scale = rec.get("scale_yi")
    price = rec.get("price")
    shares = (scale / price) if (scale and price) else None   # 亿份 = 亿元 / 元
    return {
        "amount": amt,
        "avg20": avg20,
        "mult": mult,
        "ratio_mode": ratio_mode,
        "active_net": active_net,
        "shares": shares,
        "pct": rec.get("pct"),
        "scale": scale,
        "hhmm": hhmm,
    }


def build(day_key: str | None = None) -> dict:
    hist = load()
    day_key = day_key or sorted(hist)[-1]
    snap = hist[day_key]
    pool = snap["pool"]
    curves = {r["code"]: intraday_curve(r) for r in pool}

    rows = []
    for rec in pool:
        r = dict(rec)
        r["m"] = compute_row(rec, curves[rec["code"]])
        rows.append(r)

    focus = [r for r in rows if r["tier"] == "focus"]
    watch = [r for r in rows if r["tier"] == "watch"]

    def agg(rs):
        amt = sum(r["m"]["amount"] for r in rs)
        wsum = sum(r["m"]["amount"] for r in rs if r["m"]["active_net"] is not None)
        net = sum(r["m"]["active_net"] for r in rs if r["m"]["active_net"] is not None)
        avgs = [r["m"]["avg20"] for r in rs if r["m"]["avg20"]]
        avg = sum(avgs) if len(avgs) == len(rs) else None
        sc = sum(r["m"]["scale"] for r in rs if r["m"]["scale"])
        # 规模加权涨跌
        w = [r for r in rs if r["m"]["pct"] is not None and r["m"]["scale"]]
        pct = (sum(r["m"]["pct"] * r["m"]["scale"] for r in w) / sum(r["m"]["scale"] for r in w)) if w else None
        return {"amount": amt, "avg20": avg, "mult": round(amt / avg, 2) if avg else None,
                "active_net": net if wsum else None, "scale": sc, "pct": pct,
                "net_covered": wsum / amt if amt else None}

    A = agg(focus)

    # 份额时序（focus 池合计）
    share_series = []
    for d in sorted(hist):
        s = hist[d]
        tot = 0.0
        n = 0
        for r in s["pool"]:
            if r["tier"] != "focus":
                continue
            sc, pr = r.get("scale_yi"), r.get("price")
            if sc and pr:
                tot += sc / pr
                n += 1
        if n >= len(focus) * 0.8:
            share_series.append({"date": d, "shares": tot})
    # 加仓指数
    if share_series:
        base = share_series[0]["shares"]
        for p in share_series:
            p["index"] = round(100 * p["shares"] / base, 2) if base else None
            p["delta"] = None
    for i in range(1, len(share_series)):
        share_series[i]["delta"] = round(share_series[i]["shares"] - share_series[i - 1]["shares"], 2)

    prev_snap_date = None
    dates = sorted(hist)
    idx = dates.index(day_key)
    if idx > 0:
        prev_snap_date = dates[idx - 1]
        prev = hist[prev_snap_date]
        prevnet = {}
        for r in prev["pool"]:
            o, i2 = r.get("outer"), r.get("inner")
            if o and i2 and (o + i2) > 0 and r.get("amount_yuan"):
                prevnet[r["code"]] = (o - i2) / (o + i2) * r["amount_yuan"]
        for r in rows:
            p = prevnet.get(r["code"])
            r["m"]["active_net_prev"] = p
            r["m"]["active_net_chg"] = (r["m"]["active_net"] - p) if (p is not None and r["m"]["active_net"] is not None) else None

    return {
        "day": day_key,
        "generated_at": snap["fetched_at"],
        "degraded": snap.get("degraded", []),
        "rows": rows,
        "focus": focus,
        "watch": watch,
        "agg": A,
        "share_series": share_series,
        "prev_snap": prev_snap_date,
        "session_end": max((r["m"]["hhmm"] for r in rows), default="1500") >= "1500",
        "closes": {"510300": _index_close(rows)},
    }


def _index_close(rows):
    for r in rows:
        if r["code"] == "510300" and r.get("daily"):
            return r["daily"][-1]["close"]
    return None


if __name__ == "__main__":
    b = build()
    print("day", b["day"], "generated", b["generated_at"])
    print("focus agg", {k: (round(v, 2) if isinstance(v, float) else v) for k, v in b["agg"].items()})
    for r in b["focus"][:4]:
        m = r["m"]
        print(r["code"], r["name"], "额%.1f亿" % (m["amount"] / 1e8), "均%.1f亿" % ((m["avg20"] or 0) / 1e8),
              "倍数", m["mult"], "主动净买%.2f亿" % ((m["active_net"] or 0) / 1e8))
    print("share_series", b["share_series"])
