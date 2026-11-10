"""历史净申购序列：用官方份额回补数据算真实的资金流。

与 metrics.share_series 的区别：
- metrics 从 history.json（每日快照）算，依赖当天跑过程序；
- 本模块从 shares_official.json（交易所回补）算，可覆盖几十个交易日。

口径（和 metrics 保持一致）：
    F_t = Σ_i (S_i,t − S_i,t−1) × P_i,t
只让份额变化决定正负，价格只做单位换算。
注意绝不能用「总市值变化」代替：份额没动、价格上涨也会凭空造出资金流。

覆盖率是这里最重要的输出：
沪市份额可回溯，深市不可。所以历史区间内 official 覆盖率大约只有 15/24。
缺份额的标的不计入该日 flow，同时记录 flow_n，避免把「缺数据」读成「没资金流」。
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = HERE
DAILY_DIR = os.path.join(SNAP, "daily")
SHARE_PATH = os.path.join(SNAP, "shares_official.json")
POOL_PATH = os.path.join(SNAP, "pool.json")


def _load(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def load_pool():
    return _load(POOL_PATH, [])


def load_daily_map(pool) -> dict:
    """{code: {date: bar}}，bar 含 close / amount / close_qfq。"""
    out = {}
    for p in pool:
        bars = _load(os.path.join(DAILY_DIR, f"{p['code']}.json"), [])
        out[p["code"]] = {b["date"]: b for b in bars}
    return out


def build_flow(pool=None, tier="focus", start=None, end=None) -> dict:
    """按日期返回净申购金额与覆盖率。

    返回:
        days: [{date, flow, flow_n, official_ratio, prices_ok}]
        by_index: [{date, index, flow, n}]
        meta: {...}
    """
    pool = pool or load_pool()
    shares = _load(SHARE_PATH, {})
    daily = load_daily_map(pool)
    if tier:
        pool = [p for p in pool if p.get("tier") == tier]
    codes = [p["code"] for p in pool]
    idx_of = {p["code"]: p["index"] for p in pool}

    # 交易日 = 日K 覆盖的日期（行情最可靠，不用手写日历）
    dates = sorted({d for c in codes for d in daily.get(c, {})})
    dates = [d for d in dates if (start or "") <= d <= (end or "9999")]

    days = []
    prev_shares = None
    prev_date = None
    for d in dates:
        # 当日官方份额
        # 只留池内标的：shares_official.json 存的是全市场快照
        cur = {c: v for c, v in (shares.get(d) or {}).items() if c in idx_of}
        # 分母：当日既有日K又有份额的标的（缺行情也算不出金额，不该计入分母）
        comparable = sum(1 for c in codes
                         if daily.get(c, {}).get(d) and cur.get(c))
        flow = 0.0
        n_flow = 0
        per_index = {}
        prices_ok = 0
        for c in codes:
            bar = daily.get(c, {}).get(d)
            price = bar.get("close") if bar else None          # 不复权收盘价
            if not (price and cur.get(c)):
                continue
            prices_ok += 1
            if prev_shares is None:
                continue
            psh = prev_shares.get(c)
            if psh is None:
                continue
            f = (cur[c]["shares"] - psh) * price                # 亿份 × 元 = 亿元
            flow += f
            n_flow += 1
            ix = idx_of[c]
            e = per_index.setdefault(ix, {"flow": 0.0, "n": 0})
            e["flow"] += f
            e["n"] += 1
        days.append({
            "date": d,
            "flow": round(flow, 3) if n_flow else None,
            "flow_n": n_flow,
            # 分母是「当日有行情的 focus 标的」，不是全部 focus：
            # 深市标的永远不会有官方份额，拿 24 做分母会让覆盖率恒为 0.75，掩盖真实缺口
            # official_ratio：在有行情的标的里，有多少拿到了官方份额。
            # 深市标的永远拿不到历史份额，所以这里恒等于沪市占比（focus 池约 13/20）。
            "official_ratio": round(len(cur) / prices_ok, 3) if prices_ok else None,
            "covered_n": len(cur),
            "priced_n": prices_ok,
            "comparable_n": comparable,
            "prices_ok": prices_ok,
        })
        if per_index:
            days[-1]["by_index"] = {k: {"flow": round(v["flow"], 3), "n": v["n"]}
                                    for k, v in per_index.items()}
        prev_shares = {c: cur[c]["shares"] for c in cur}
        prev_date = d

    # 累计
    cum = 0.0
    for d in days:
        if d["flow"] is not None:
            cum += d["flow"]
        d["cum_flow"] = round(cum, 3)

    usable = [d for d in days if d["flow"] is not None]
    return {
        "days": days,
        "meta": {
            "pool_n": len(codes),
            "dates": len(dates),
            "first": dates[0] if dates else None,
            "last": dates[-1] if dates else None,
            "flow_days": len(usable),
            "note": "深交所份额不可回溯：历史区间只有沪市标的有官方份额，"
                    "flow 仅覆盖这部分（focus 池 13/20），不是全池口径",
        },
    }


if __name__ == "__main__":
    b = build_flow()
    m = b["meta"]
    print(f"区间 {m['first']} ~ {m['last']}，交易日 {m['dates']}，可算 flow {m['flow_days']} 天")
    for d in b["days"][-8:]:
        if d["flow"] is None:
            print(f"  {d['date']}  flow= n/a  覆盖{d['official_ratio']}")
        else:
            print(f"  {d['date']}  flow={d['flow']:+8.2f} 亿  n={d['flow_n']:2}  "
                  f"覆盖{d['official_ratio']}  累计{d['cum_flow']:+8.2f}")
