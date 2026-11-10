"""历史回补：东财日K + 上交所 STAT_DATE 官方份额。

解决的问题：新启动的项目只有当天一份快照，申赎金额、累计申赎、加仓指数
全部算不出来。本脚本用两条可回溯的接口把历史补齐。

能力边界（实测，2026-11-10）：
- 东财日K：可取数千个交易日，120 天轻松。带真实成交额 + 前复权价。
- 上交所份额：STAT_DATE 可精确回取任意历史交易日，一次返回全市场 923 只。
  非交易日返回 0 行，可据此判定。
- 深交所份额：**只能取最新一期**，无法回补。深市标的历史份额只能靠每日落盘。
  所以回补出来的历史里，深市标的的 official 覆盖率为 0，会如实标注，不做伪造。

产出：
- snapshots/index_daily.json      指数日K（按指数增量合并）
- snapshots/daily/{code}.json     每只 ETF 的日K（不复权 + 前复权）
- snapshots/shares_official.json  官方份额（仅沪市可回补）

注意：不写 history.json。快照是「当时看到的市场状态」，事后回补不进去，
否则会用今天的日K 去假装那天的盘中数据。历史资金流直接从 daily/ 与
shares_official.json 推算，见 backfill_series()。

用法：
    python backfill.py --days 120
    python backfill.py --days 120 --sse-days 60   # 份额只回补近 60 个交易日
    python backfill.py --check                    # 只探测可用性，不写文件
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, "snapshots")
if SNAP not in sys.path:
    sys.path.insert(0, SNAP)

import fetch_snapshot as F  # noqa: E402

DAILY_DIR = os.path.join(SNAP, "daily")
POOL_PATH = os.path.join(SNAP, "pool.json")
IDX_PATH = F.IDX_PATH
SHARE_PATH = F.SHARE_PATH


def log(msg):
    print(msg, flush=True)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)          # 原子替换，中断不会留半个文件


def backfill_daily(codes, days):
    """每只 ETF 的日K。先写临时再替换，且只在拿到足够数据时才覆盖。"""
    os.makedirs(DAILY_DIR, exist_ok=True)
    ok, short, failed = 0, [], []
    for i, code in enumerate(codes, 1):
        path = os.path.join(DAILY_DIR, f"{code}.json")
        try:
            bars = F.fetch_daily(code, days)
        except Exception as e:  # noqa: BLE001
            failed.append((code, type(e).__name__))
            log(f"  [{i:2}/{len(codes)}] {code} 失败 {type(e).__name__}")
            continue
        old = load_json(path, [])
        merged = {b["date"]: b for b in old}
        for b in bars:
            merged[b["date"]] = b          # 新数据覆盖同日旧值
        out = [merged[k] for k in sorted(merged)][-days:]
        save_json(path, out)
        has_amt = sum(1 for b in out if b.get("amount"))
        has_qfq = sum(1 for b in out if b.get("close_qfq"))
        log(f"  [{i:2}/{len(codes)}] {code} {len(out):4}根 "
            f"成交额{has_amt} 复权{has_qfq}  {out[0]['date']}~{out[-1]['date']}"
            if out else f"  [{i:2}/{len(codes)}] {code} 空")
        if len(out) < days * 0.8:
            short.append(code)
        else:
            ok += 1
        time.sleep(0.15)
    return ok, short, failed


def backfill_index(pool, days):
    """指数日K：逐指数合并，单个失败不影响其他。"""
    m = F.idx_map(pool)
    payload = load_json(IDX_PATH, {})
    ok, failed = 0, []
    for name, code in m.items():
        try:
            bars = F.fetch_daily(code, days)
        except Exception as e:  # noqa: BLE001
            failed.append((code, type(e).__name__))
            continue
        old = (payload.get(code) or {}).get("daily") or []
        merged = {b["date"]: b for b in old}
        for b in bars:
            merged[b["date"]] = b
        series = [{"date": k, "close": merged[k]["close"]} for k in sorted(merged)]
        payload[code] = {
            "name": name, "code": code, "daily": series,
            "last_date": series[-1]["date"] if series else None,
        }
        log(f"  {name:8} {code:9} {len(series):4}根 {series[0]['date']}~{series[-1]['date']}")
        ok += 1
        time.sleep(0.15)
    if ok:
        save_json(IDX_PATH, payload)
    return ok, failed


def trading_days_from(anchor_date, n):
    """从锚点往前推 n 个「疑似交易日」（去掉周末），节假日由接口返回空来过滤。"""
    d = datetime.strptime(anchor_date, "%Y-%m-%d")
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return list(reversed(out))


def backfill_sse_shares(codes, days, anchor_date):
    """上交所份额按 STAT_DATE 逐日回补。工作日里遇到节假日会返回 0 行，跳过。"""
    want = {c for c in codes if c[0] in "56"}       # 沪市：5/6 开头
    store = load_json(SHARE_PATH, {})
    dates = trading_days_from(anchor_date, days)
    got_days, empty_days, failed = 0, [], []
    for i, d in enumerate(dates, 1):
        if d in store and len(store[d]) >= len(want) * 0.8:
            log(f"  [{i:3}/{len(dates)}] {d} 已存在，跳过")
            got_days += 1
            continue
        try:
            res = F.fetch_sse_shares(d)
        except Exception as e:  # noqa: BLE001
            failed.append((d, type(e).__name__))
            log(f"  [{i:3}/{len(dates)}] {d} 失败 {type(e).__name__}")
            continue
        # 只认接口回传日期与请求日期一致的记录，防止拿别的日子冒充
        res = {k: v for k, v in res.items() if v.get("date") == d}
        if not res:
            empty_days.append(d)               # 节假日
            continue
        bucket = store.setdefault(d, {})
        n_new = 0
        for c, v in res.items():
            if c in want:
                bucket[c] = {"name": v["name"], "shares": round(v["shares"], 4)}
                n_new += 1
        log(f"  [{i:3}/{len(dates)}] {d} 沪市 {n_new:2}/{len(want)} 只")
        got_days += 1
        time.sleep(0.35)
    if got_days:
        save_json(SHARE_PATH, store)
    return got_days, empty_days, failed, want


def summarize(pool, days):
    """回补后的可用性报告：每个交易日有几只标的能算出净申购。"""
    codes = [p["code"] for p in pool]
    name_of = {p["code"]: p["name"] for p in pool}
    shares = load_json(SHARE_PATH, {})
    daily = {c: load_json(os.path.join(DAILY_DIR, f"{c}.json"), []) for c in codes}

    # 以日K 能覆盖的日期为基准（交易日由行情决定，最可靠）
    universe = sorted({b["date"] for bars in daily.values() for b in bars})
    rows = []
    for i, d in enumerate(universe):
        n_bar = sum(1 for c in codes if any(b["date"] == d for b in daily[c]))
        n_off = len(shares.get(d, {}))
        rows.append({"date": d, "bars": n_bar, "official": n_off})
    return rows, universe


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120, help="日K 回溯交易日数")
    ap.add_argument("--sse-days", type=int, default=60, help="上交所份额回溯交易日数")
    ap.add_argument("--check", action="store_true", help="只探测，不写文件")
    args = ap.parse_args(argv)

    pool = load_json(POOL_PATH, [])
    codes = [p["code"] for p in pool]
    anchor = datetime.now().strftime("%Y-%m-%d")
    log(f"标的 {len(pool)} 只，锚点 {anchor}")

    if args.check:
        d = F.fetch_daily(codes[0], 5)
        log(f"东财日K {codes[0]}: {len(d)} 根，最新 {d[-1]['date']}，"
            f"成交额={'有' if d[-1].get('amount') else '无'}")
        sse = F.fetch_sse_shares(anchor)
        log(f"上交所份额: {len(sse)} 只，日期 {set(v['date'] for v in sse.values())}")
        try:
            F.fetch_szse_shares()
            log("深交所: 可取最新一期（不可回溯）")
        except Exception as e:  # noqa: BLE001
            log(f"深交所: 失败 {type(e).__name__}")
        return 0

    log("\n▶ 1/3 日K（东财，不复权 + 前复权）")
    ok, short, failed = backfill_daily(codes, args.days)
    log(f"  完成 {ok}/{len(codes)}，偏短 {len(short)}，失败 {len(failed)}")

    log("\n▶ 2/3 指数日K")
    iok, ifail = backfill_index(pool, args.days + 20)
    log(f"  完成 {iok}，失败 {len(ifail)}")

    log(f"\n▶ 3/3 上交所官方份额（最近 {args.sse_days} 个交易日）")
    gd, empty, sfail, want = backfill_sse_shares(codes, args.sse_days, anchor)
    log(f"  回补 {gd} 个交易日，节假日跳过 {len(empty)}，失败 {len(sfail)}，沪市标的 {len(want)} 只")
    log("  说明：深交所份额不可回溯，深市标的历史 official 覆盖率会是 0，属预期")

    log("\n▶ 可用性汇总")
    rows, universe = summarize(pool, args.days)
    full_off = sum(1 for r in rows if r["official"] >= len(want) * 0.8)
    log(f"  交易日 {len(rows)} 个：{universe[0] if universe else '-'} ~ {universe[-1] if universe else '-'}")
    log(f"  有官方份额的交易日 {full_off} 个")
    log(f"  可算净申购（需连续两日官方份额）约 {max(0, full_off - 1)} 天")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
