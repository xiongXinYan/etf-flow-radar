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
IDX_PATH = os.path.join(SNAP_DIR, "index_daily.json")

# 指数代码映射：采集与超额收益计算共用同一份，避免两处各写一遍
IDX_MAP = {
    "沪深300": "sh000300", "上证50": "sh000016", "中证1000": "sh000852",
    "中证500": "sh000905", "科创50": "sh000688", "创业板指": "sz399006",
    "上证180": "sh000010", "上证380": "sh000009", "中证800": "sh000906",
    "深证100": "sz399330", "中证A500": "sh000510", "上证180金融": "sh000018",
}


def idx_map(pool) -> dict:
    """池内实际用到的「指数名 -> 指数代码」。"""
    names = {p["index"] for p in pool}
    return {n: IDX_MAP[n] for n in names if n in IDX_MAP}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Referer": "https://finance.sina.com.cn",
}


def sym(code: str) -> str:
    """6 位代码补市场前缀；已经带 sh/sz 的（指数）原样返回。"""
    if code[:2] in ("sh", "sz"):
        return code
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
    """腾讯实时：返回 {code: {price, volume_shares, amount_yuan, pct, prev_close, ...}}

    字段口径（实测，2026-11-10 逐项核对过）：
    - [3]  现价
    - [6]  成交量（单位是「手」，1 手 = 100 份，ETF 与股票口径一致）
    - [30] 时间戳，格式为 yyyymmddHHMMSS（如 20261110161436），不是 "2026-11-10 16:14:36"
    - [37] 成交额（万元）
    - [45] 流通市值（亿元），≈ [6]×100×[3]，即「成交量×现价」，不是基金公司披露的规模
    """
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
                "volume_lots": _f(payload, 6),                  # 成交量（手）
                "volume_shares": float(payload[6]) * 100,      # 手 -> 份
                "amount_yuan": float(payload[37]) * 1e4,        # 万元 -> 元
                "high": float(payload[33]),
                "low": float(payload[34]),
                # 以下是可选字段，取不到给 None
                "outer": _f(payload, 7),     # 外盘（手）
                "inner": _f(payload, 8),     # 内盘（手）
                "turnover_rate": _f(payload, 38),
                "scale_yi": _f(payload, 45),                 # 行情口径市值(亿元)=成交量×现价
            }
            # 份额：用「成交量(份) ÷ 换手率」反推总份额，而不是「市值 ÷ 价格」
            # （后者对 1.0 元附近的面值类 ETF 在低价时误差可达 2~3 倍）
            tn = out[code]["turnover_rate"]
            vols = out[code]["volume_shares"]
            out[code]["total_shares"] = (vols / (tn / 100.0)) if (tn and tn > 0 and vols) else None
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
    """不复权日K。为什么必须不复权：

    腾讯同时提供 qfq（前复权）和 day（不复权）两套价格，同一天 510300 的
    qfq 收盘是 4.14、不复权是 4.135。用 qfq 收盘乘「不复权成交量」算历史成交额，
    在发生过分红/折算的标的上会系统性偏移；成交额必须用真实成交价算。
    """
    s = sym(code)
    u = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={s},day,,,{days},"
    j = get(u).json()
    d = j["data"][s]
    k = d.get("day") or []
    # [date, open, close, high, low, volume(手)]
    return [
        {"date": row[0], "open": float(row[1]), "close": float(row[2]),
         "high": float(row[3]), "low": float(row[4]), "volume_lots": float(row[5])}
        for row in k
    ]


def fetch_index_daily(codes, days=120):
    """指数不复权日K，用于超额收益。与 ETF 一样取 day 而非 qfq。"""
    out = {}
    for c in codes:
        try:
            out[c] = fetch_daily(c, days)
        except Exception:  # noqa: BLE001
            out[c] = []
    return out


def fetch_min5(code, n=1200):
    """5 分钟K。

    实测腾讯 m5 接口无论 n 传多少，最多只回 320 根 ≈ 6.7 个交易日
    （每日 48 根），所以「20 日同时点曲线」目前实际只有约 7 个样本。
    要更长必须每日落地累积，见 merge_m5()。
    """
    s = sym(code)
    u = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={s},m5,,{n}"
    j = get(u).json()
    d = j["data"][s].get("m5", [])
    # [yyyymmddHHMM, open, close, high, low, volume(手)]
    return [
        {"t": row[0], "close": float(row[2]), "volume_lots": float(row[5])}
        for row in d
    ]


M5_DIR = os.path.join(SNAP_DIR, "m5")


def merge_m5(code: str, bars, keep_days: int = 30):
    """把当日拉到的 5 分钟K 并入按标的累积的文件，保留最近 keep_days 个交易日。

    接口一次只给 320 根（约 6.7 日），不落盘累积就永远凑不够 20 日样本，
    同时点曲线会长期被最近一周的成交节奏主导。
    """
    os.makedirs(M5_DIR, exist_ok=True)
    path = os.path.join(M5_DIR, f"{code}.json")
    old = []
    if os.path.exists(path):
        try:
            old = json.load(open(path, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            old = []
    merged = {b["t"]: b for b in old}
    for b in bars:
        merged[b["t"]] = b          # 新数据覆盖同日旧值（盘中刷新）
    out = [merged[k] for k in sorted(merged)]
    days = sorted({b["t"][:8] for b in out})
    cut = set(days[-keep_days:])
    out = [b for b in out if b["t"][:8] in cut]
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    return out


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
            rec["m5"] = merge_m5(c, fetch_min5(c))
        except Exception as e:  # noqa: BLE001
            rec["m5"] = []
            row["degraded"].append(f"{c}:min5:{type(e).__name__}")
        row["pool"].append(rec)

    # 指数日K：之前 index_daily.json 没有任何采集代码更新，超额收益会长期吃旧数据
    try:
        m = idx_map(pool)
        idx = fetch_index_daily(sorted(m.values()), 120)
        payload = {
            code: {"name": name, "code": code, "daily": idx.get(code, [])}
            for name, code in m.items()
        }
        # 只有拿到数据才覆盖，避免一次网络失败把历史清空
        payload = {k: v for k, v in payload.items() if v["daily"]}
        if payload:
            json.dump(payload, open(IDX_PATH, "w", encoding="utf-8"), ensure_ascii=False)
        else:
            row["degraded"].append("index:empty")
    except Exception as e:  # noqa: BLE001
        row["degraded"].append(f"index:{type(e).__name__}")
    hist[today] = row
    json.dump(hist, open(HIST_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    total = sum(r.get("amount_yuan") or 0 for r in row["pool"] if r["tier"] == "focus")
    print(f"saved {today}: {len(row['pool'])} ETFs, focus amount {total/1e8:.1f}亿, degraded {len(row['degraded'])}")


if __name__ == "__main__":
    main(check_only="--check" in sys.argv)
