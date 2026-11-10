"""每日快照采集：实时行情 + 日K + 5分钟K + 份额，落盘到 snapshots/。

设计要点
- 只做采集与计算，不做结论；所有字段保留原始值，便于复核。
- 每个交易日只写一行，重复运行则更新当日行（盘中刷新用）。
- 新浪 5 分钟K / 腾讯日K 失败时不覆盖历史，标记 degraded。

用法：
    python fetch_snapshot.py          # 采集并写入
    python fetch_snapshot.py --check  # 只检查接口连通性
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_DIR = os.path.join(ROOT, "snapshots")
POOL_PATH = os.path.join(SNAP_DIR, "pool.json")
HIST_PATH = os.path.join(SNAP_DIR, "history.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Referer": "https://finance.sina.com.cn",
}


def sym(code: str) -> str:
    return ("sh" if code[0] in "56" else "sz") + code


def get(url: str, **kw) -> requests.Response:
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20, **kw)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def fetch_realtime(codes):
    """腾讯实时：返回 {code: {price, volume_shares, amount_yuan, pct, prev_close, ...}}"""
    q = ",".join(sym(c) for c in codes)
    r = get("https://qt.gtimg.cn/q=" + q)
    r.encoding = "gbk"
    out = {}
    for line in r.text.split(";"):
        if "=" not in line:
            continue
        payload = line.split('="', 1)[1].strip('"\n').split("~")
        if len(payload) < 46:
            continue
        code = payload[2]
        try:
            out[code] = {
                "name": payload[1],
                "price": float(payload[3]),
                "prev_close": float(payload[4]),
                "pct": float(payload[32]),
                "time": payload[30],
                "volume_shares": float(payload[6]) * 100,      # 手 -> 股
                "amount_yuan": float(payload[37]) * 1e4,        # 万元 -> 元
                "high": float(payload[33]),
                "low": float(payload[34]),
                # 以下是可选字段，取不到给 None
                "outer": _f(payload, 7),     # 外盘（手）
                "inner": _f(payload, 8),     # 内盘（手）
                "turnover_rate": _f(payload, 38),
                "scale_yi": _f(payload, 45),                 # 行情口径规模(亿元)
            }
        except (ValueError, IndexError):
            continue
    return out


def _f(payload, i):
    try:
        v = payload[i]
        return float(v) if v not in ("", "-") else None
    except (IndexError, ValueError):
        return None


def fetch_daily(code, days=60):
    s = sym(code)
    u = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={s},day,,,{days},qfq"
    j = get(u).json()
    d = j["data"][s]
    k = d.get("qfqday") or d.get("day") or []
    # [date, open, close, high, low, volume(手)]
    return [
        {"date": row[0], "open": float(row[1]), "close": float(row[2]),
         "high": float(row[3]), "low": float(row[4]), "volume_lots": float(row[5])}
        for row in k
    ]


def fetch_min5(code, n=320):
    s = sym(code)
    u = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={s},m5,,{n}"
    j = get(u).json()
    d = j["data"][s].get("m5", [])
    # [yyyymmddHHMM, open, close, high, low, volume(手)]
    return [
        {"t": row[0], "close": float(row[2]), "volume_lots": float(row[5])}
        for row in d
    ]


def top_movers(codes):
    """扩展观察：全市场同指数 ETF 成交额TOP（用于发现池外异常放量）。"""
    return []


def main(check_only=False):
    pool = json.load(open(POOL_PATH, encoding="utf-8"))
    codes = [p["code"] for p in pool]
    name_of = {p["code"]: p["name"] for p in pool}

    rt = fetch_realtime(codes)
    if check_only:
        print(f"realtime ok: {len(rt)}/{len(codes)}")
        one = fetch_daily(codes[0], 5)
        print("daily ok:", len(one), one[-1])
        m = fetch_min5(codes[0], 40)
        print("min5 ok:", len(m))
        return

    hist = {}
    if os.path.exists(HIST_PATH):
        hist = json.load(open(HIST_PATH, encoding="utf-8"))

    today = datetime.now().strftime("%Y-%m-%d")
    row = {
        "date": today,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "pool": [],
        "degraded": [],
    }
    for p in pool:
        c = p["code"]
        rec = dict(p)
        rec.update(rt.get(c, {}))
        try:
            rec["daily"] = fetch_daily(c, 45)
        except Exception as e:  # noqa: BLE001
            rec["daily"] = []
            row["degraded"].append(f"{c}:daily:{type(e).__name__}")
        try:
            rec["m5"] = fetch_min5(c, 320)
        except Exception as e:  # noqa: BLE001
            rec["m5"] = []
            row["degraded"].append(f"{c}:min5:{type(e).__name__}")
        row["pool"].append(rec)
    hist[today] = row
    json.dump(hist, open(HIST_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    total = sum(r.get("amount_yuan") or 0 for r in row["pool"] if r["tier"] == "focus")
    print(f"saved {today}: {len(row['pool'])} ETFs, focus amount {total/1e8:.1f}亿, degraded {len(row['degraded'])}")


if __name__ == "__main__":
    main(check_only="--check" in sys.argv)
