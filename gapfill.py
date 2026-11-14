"""启动时的历史缺口检查与自动回补。

解决的问题：程序只在运行时记录当天。一旦隔了几天没启动，中间那些交易日就
整段消失——history.json 少几行、daily/ 的日K 停在过去、shares_official.json
少几个日期，页面上的历史净申购曲线因此断掉。

本模块在采集之前先回答两个问题：
  1. 最近 N 个交易日里，哪一天、哪个数据源是缺的？
  2. 哪些能补、补回来没有？

交易日历来自行情日K（默认沪深300指数），不靠「工作日 ≠ 节假日」硬猜：
行情里出现的日期就是交易日，节假日自然不在里面。

能补的：
  - daily/{code}.json     东财日K 可回溯数千个交易日，整段重拉后合并
  - index_daily.json      同上，按指数增量合并
  - shares_official.json  上交所 STAT_DATE 可精确回取任意历史交易日

补不回来的，如实标注，绝不用今天的数据去填过去的坑：
  - history.json          盘中实时快照 / 5 分钟K 已经过去，无法事后重建
  - 深交所历史份额        接口只给最新一期，历史只能靠每日运行累积
  - m5/                   腾讯只回溯约 320 根（≈6.7 个交易日）

用法：
    python gapfill.py                 # 检查并回补最近 20 个交易日
    python gapfill.py --check         # 只检查，不写任何文件
    python gapfill.py --days 60       # 扩大检查窗口

产出：snapshots/gap_report.json（原子写，记录本次检查与回补结果）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, "snapshots")
for _p in (HERE, SNAP):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import backfill as B  # noqa: E402  复用 load_json / save_json / trading_days_from
import fetch_snapshot as F  # noqa: E402

REPORT_PATH = os.path.join(SNAP, "gap_report.json")
CALENDAR_CODE = "sh000300"      # 指数比单只 ETF 稳定：不会因停牌少一天
DEFAULT_DAYS = 20
REQUEST_GAP = 0.15              # 与 backfill.py 一致；东财未观察到 WAF 问题
M5_LOOKBACK = 7                 # 腾讯 m5 只回溯约 6.7 个交易日
MAX_BARS = 400                  # daily/ 保留上限，避免多年后文件无限膨胀


def log(msg):
    print(msg, flush=True)


def trading_calendar(days: int, today: str) -> tuple[list[str], str]:
    """最近 days 个「已过去的交易日」。今天由当日采集负责，不进回补窗口。

    优先用指数日K 的真实日期；接口全挂时退化为「工作日」猜测并标明来源，
    此时节假日可能混进来，由上交所返回 0 行来兜底过滤。
    """
    try:
        bars = F.fetch_daily(CALENDAR_CODE, days + 10)
        dates = sorted({b["date"] for b in bars if b.get("date", "") < today})
        if len(dates) >= 3:
            return dates[-days:], "eastmoney_index_kline"
    except Exception as e:  # noqa: BLE001
        log(f"  ! 指数日K 日历不可用（{type(e).__name__}），改用工作日推断")
    guess = [d for d in B.trading_days_from(today, days + 5) if d < today]
    return guess[-days:], "weekday_guess"


def _m5_date(t) -> str | None:
    t = str(t or "")[:8]
    return f"{t[:4]}-{t[4:6]}-{t[6:8]}" if len(t) == 8 and t.isdigit() else None


def inspect(pool, codes, window) -> dict:
    """只读检查：逐源列出窗口内缺哪些日期。"""
    # 1) 每只 ETF 的日K
    daily_gaps = {}
    for c in codes:
        bars = B.load_json(os.path.join(B.DAILY_DIR, f"{c}.json"), [])
        have = {b.get("date") for b in bars}
        miss = [d for d in window if d not in have]
        if miss:
            daily_gaps[c] = miss

    # 2) 指数日K
    idx_payload = B.load_json(F.IDX_PATH, {})
    index_gaps = {}
    for code in F.idx_map(pool).values():
        have = {x.get("date") for x in (idx_payload.get(code) or {}).get("daily") or []}
        miss = [d for d in window if d not in have]
        if miss:
            index_gaps[code] = miss

    # 3) 官方份额：按「池内该市场代码数」判断，而不是看日期键在不在
    #    （某天可能只有深市那一期，沪市仍缺）
    sse_codes = [c for c in codes if c[0] in "56"]
    szse_codes = [c for c in codes if c not in sse_codes]
    store = B.load_json(F.SHARE_PATH, {})

    def missing_share_days(subset):
        threshold = max(1, int(len(subset) * 0.8))
        return [d for d in window
                if sum(1 for c in subset if c in (store.get(d) or {})) < threshold]

    sse_missing = missing_share_days(sse_codes)
    szse_missing = missing_share_days(szse_codes)

    # 4) 盘中快照（不可补，仅报告）
    #    只从「第一份快照」之后算缺口。backfill.py 明确不写 history.json，
    #    所以首批快照之前的日子是设计使然，不是「漏跑」，列出来只会淹没真缺口。
    hist = B.load_json(F.HIST_PATH, {})
    hist_missing, snapshot_note = [], None
    if hist:
        baseline = min(hist)
        hist_missing = [d for d in window if d >= baseline and d not in hist]
    else:
        snapshot_note = "history.json 尚无快照，跳过盘中快照缺口判定（首次运行）"

    # 5) 5 分钟K：只统计最近 7 个交易日的覆盖，不列入「缺口」清单
    m5_window = window[-M5_LOOKBACK:]
    m5_cover = []
    for c in codes:
        bars = B.load_json(os.path.join(F.M5_DIR, f"{c}.json"), [])
        have = {_m5_date(b.get("t")) for b in bars}
        m5_cover.append(sum(1 for d in m5_window if d in have))

    return {
        "sse_codes": sse_codes,
        "daily": daily_gaps,
        "index": index_gaps,
        "sse_shares": sse_missing,
        "szse_shares": szse_missing,
        "history": hist_missing,
        "history_baseline": min(hist) if hist else None,
        "snapshot_note": snapshot_note,
        "history_extra_rows": sorted(d for d in hist if window and d > window[-1]),
        "m5": {
            "window": m5_window,
            "avg_days": round(sum(m5_cover) / len(m5_cover), 1) if m5_cover else 0,
            "codes_full": sum(1 for n in m5_cover if n == len(m5_window)),
        },
    }


def by_date(window, ins) -> dict:
    """按交易日汇总：这一天到底缺了什么。给用户看的主视图。"""
    out = {}
    for d in window:
        out[d] = {
            "daily": sum(1 for miss in ins["daily"].values() if d in miss),
            "index": sum(1 for miss in ins["index"].values() if d in miss),
            "sse_shares": d in ins["sse_shares"],
            "szse_shares": d in ins["szse_shares"],
            "history": d in ins["history"],
        }
    return out


def fill_daily(daily_gaps, window) -> dict:
    """东财日K 重拉 + 合并。只在拿到数据后才覆盖文件。

    刻意不把历史往前扩：起点保持原文件最早日期，只往后追加缺失的交易日。
    否则一次补 3 天会顺带多拉几十天日K，flow_history 的区间起点无故变早，
    而多出来的日子并没有份额数据，只会多一排 flow=null。
    """
    got_dates: set[str] = set()
    still, failed = {}, []
    for c, miss in daily_gaps.items():
        path = os.path.join(B.DAILY_DIR, f"{c}.json")
        old = B.load_json(path, [])
        earliest = min((b["date"] for b in old if b.get("date")), default=None)
        lmt = max(len(old) + len(miss) + 2, len(window) + 5, 30)
        try:
            bars = F.fetch_daily(c, lmt)
        except Exception as e:  # noqa: BLE001
            failed.append({"code": c, "error": type(e).__name__})
            continue
        merged = {b["date"]: b for b in old if b.get("date")}
        for b in bars:
            if b.get("date"):
                merged[b["date"]] = b        # 新数据覆盖同日旧值（含盘中未定稿值）
        keys = [k for k in sorted(merged) if earliest is None or k >= earliest]
        B.save_json(path, [merged[k] for k in keys][-MAX_BARS:])
        got_dates.update(d for d in miss if d in merged)
        left = [d for d in miss if d not in merged]
        if left:
            still[c] = left                # 数据源本身没有该日（多为停牌）
        time.sleep(REQUEST_GAP)
    return {
        "codes": len(daily_gaps) - len(failed),
        "dates": sorted(got_dates),
        "still_missing": still,
        "failed": failed,
    }


def fill_index(index_gaps, pool, window) -> dict:
    """指数日K：逐指数合并，单个失败不影响其他。"""
    if not index_gaps:
        return {"codes": 0, "dates": [], "still_missing": {}, "failed": []}
    payload = B.load_json(F.IDX_PATH, {})
    name_of = {v: k for k, v in F.idx_map(pool).items()}
    got_dates: set[str] = set()
    still, failed = {}, []
    for code, miss in index_gaps.items():
        old = (payload.get(code) or {}).get("daily") or []
        earliest = min((x["date"] for x in old if x.get("date")), default=None)
        lmt = max(len(old) + len(miss) + 2, len(window) + 5, 30)
        try:
            bars = F.fetch_daily(code, lmt)
        except Exception as e:  # noqa: BLE001
            failed.append({"code": code, "error": type(e).__name__})
            continue
        merged = {x["date"]: x for x in old if x.get("date")}
        for b in bars:
            if b.get("date"):
                merged[b["date"]] = {"date": b["date"], "close": b["close"]}
        keys = [k for k in sorted(merged) if earliest is None or k >= earliest]
        series = [merged[k] for k in keys]
        payload[code] = {
            "name": name_of.get(code, code), "code": code,
            "daily": series, "last_date": series[-1]["date"] if series else None,
        }
        got_dates.update(d for d in miss if d in merged)
        left = [d for d in miss if d not in merged]
        if left:
            still[code] = left
        time.sleep(REQUEST_GAP)
    if got_dates:
        B.save_json(F.IDX_PATH, payload)
    return {
        "codes": len(index_gaps) - len(failed),
        "dates": sorted(got_dates),
        "still_missing": still,
        "failed": failed,
    }


def fill_sse_shares(missing, sse_codes) -> dict:
    """上交所份额按 STAT_DATE 逐日回补；只认回传日期与请求日期一致的记录。"""
    if not missing:
        return {"dates": [], "empty": [], "failed": []}
    store = B.load_json(F.SHARE_PATH, {})
    want = set(sse_codes)
    threshold = max(1, int(len(sse_codes) * 0.8))
    got, empty, failed = [], [], []
    for d in missing:
        try:
            res = F.fetch_sse_shares(d)
        except Exception as e:  # noqa: BLE001
            failed.append({"date": d, "error": type(e).__name__})
            continue
        res = {k: v for k, v in res.items()
               if v.get("date") == d and k in want}
        if len(res) < threshold:
            empty.append(d)                # 非交易日，或清算数据尚未发布
            continue
        bucket = store.setdefault(d, {})   # 不覆盖该日已有的深市条目
        for c, v in res.items():
            bucket[c] = {"name": v["name"], "shares": round(v["shares"], 4)}
        got.append(d)
        time.sleep(REQUEST_GAP)
    if got:
        B.save_json(F.SHARE_PATH, store)
    return {"dates": got, "empty": empty, "failed": failed}


def build_report(days, window, cal_src, ins, fills=None) -> dict:
    recovered_dates = set()
    if fills:
        for key in ("daily", "index"):
            recovered_dates.update(fills[key].get("dates") or [])
        recovered_dates.update(fills["sse"].get("dates") or [])

    notes = []
    if ins["history"]:
        notes.append("history.json 的盘中快照不可回补：事后的日K 无法重建当时的实时与 5 分钟数据")
    if ins.get("snapshot_note"):
        notes.append(ins["snapshot_note"])
    if ins["szse_shares"]:
        notes.append("深交所份额只提供最新一期，历史日期无法回补，只能靠每日运行累积")
    if ins["history_extra_rows"]:
        notes.append("history.json 含交易日日历之外的行（"
                     + "、".join(ins["history_extra_rows"][-5:])
                     + "），多为周末启动写入；实时行情实际是上一交易日收盘，不参与回补")
    if cal_src == "weekday_guess":
        notes.append("日历来自工作日推断，可能含节假日；已由上交所返回 0 行过滤")

    return {
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "today": datetime.now().strftime("%Y-%m-%d"),
        "mode": "fill" if fills else "check",
        "window_days": days,
        "window": [window[0], window[-1]] if window else [],
        "calendar_source": cal_src,
        "before": {
            "daily": {"codes": len(ins["daily"]),
                      "dates": sorted({d for v in ins["daily"].values() for d in v})},
            "index": {"codes": len(ins["index"]),
                      "dates": sorted({d for v in ins["index"].values() for d in v})},
            "sse_shares": ins["sse_shares"],
            "szse_shares": ins["szse_shares"],
            "history": ins["history"],
            "m5_recent": {"window": ins["m5"]["window"],
                          "avg_days": ins["m5"]["avg_days"],
                          "codes_full": ins["m5"]["codes_full"]},
        },
        "by_date": by_date(window, ins),
        "recovered": {
            "dates": sorted(recovered_dates),
            "daily": fills["daily"] if fills else None,
            "index": fills["index"] if fills else None,
            "sse_shares": fills["sse"] if fills else None,
        },
        "unrecoverable_dates": ins["history"],
        "notes": notes,
    }


def print_report(r: dict):
    w = r["window"]
    log(f"  检查区间 {w[0] if w else '-'} ~ {w[-1] if w else '-'}"
        f"（{r['window_days']} 个交易日，日历来源："
        f"{'东财指数日K' if r['calendar_source'] == 'eastmoney_index_kline' else '工作日推断'}）")

    # 主视图只列「能补的缺口」。深市历史份额与盘中快照补不回来，
    # 混进逐日清单会把真正要处理的日期淹没。
    actionable, snapshot_only = [], []
    for d, f in r["by_date"].items():
        if f["daily"] or f["index"] or f["sse_shares"]:
            actionable.append((d, f))
        elif f["history"]:
            snapshot_only.append(d)
    if not actionable:
        log("  需回补的交易日：无（日K / 指数 / 沪市份额均已齐备）")
    else:
        log(f"  需回补的交易日 {len(actionable)} 天：")
        for d, f in actionable:
            parts = []
            if f["daily"]:
                parts.append(f"日K缺{f['daily']}只")
            if f["index"]:
                parts.append(f"指数缺{f['index']}个")
            if f["sse_shares"]:
                parts.append("沪市份额缺")
            if f["history"]:
                parts.append("盘中快照缺(不可补)")
            log(f"    {d}  " + " · ".join(parts))
    if snapshot_only:
        tail = snapshot_only[-3:]
        log(f"  另有 {len(snapshot_only)} 天仅缺盘中快照（{'、'.join(tail)}"
            f"{'…' if len(snapshot_only) > 3 else ''}），按设计不回补")

    if r["mode"] == "check":
        log("  --check 模式：仅检查，未回补、未写文件")
    else:
        rec = r["recovered"]
        log(f"  回补：日K {rec['daily']['codes']} 只 · 指数 {rec['index']['codes']} 个 · "
            f"沪市份额 {len(rec['sse_shares']['dates'])} 天"
            + (f" → 共补回 {len(rec['dates'])} 天（{'、'.join(rec['dates'])}）"
               if rec["dates"] else " → 无新增日期"))
        for label, key in (("日K", "daily"), ("指数", "index")):
            still = rec[key]["still_missing"]
            if still:
                log(f"  ! {label}仍缺：{len(still)} 个标的（数据源无该日，多为停牌）")
            if rec[key]["failed"]:
                log(f"  ! {label}拉取失败：{len(rec[key]['failed'])} 个")
        sse = rec["sse_shares"]
        if sse["empty"]:
            log(f"  ! 沪市份额无数据（非交易日/未清算）：{'、'.join(sse['empty'])}")
        if sse["failed"]:
            log(f"  ! 沪市份额拉取失败：{len(sse['failed'])} 天")

    if r["unrecoverable_dates"]:
        log(f"  不可回补：盘中快照 {'、'.join(r['unrecoverable_dates'])}"
            "（实时/5分钟数据已过去，不用日K 伪造）")
    m5 = (r.get("before") or {}).get("m5_recent") or {}
    log(f"  5 分钟K：最近 {len(m5.get('window') or [])} 个交易日平均覆盖 "
        f"{m5.get('avg_days', 0)} 天（接口仅回溯约 6.7 日，更早属预期）")
    for n in r["notes"]:
        if not n.startswith("history.json 的盘中快照") and not n.startswith("深交所份额"):
            log(f"  注：{n}")


def run(days: int = DEFAULT_DAYS, check: bool = False) -> dict:
    """检查并（可选）回补。返回报告 dict；不抛异常给调用方。"""
    days = max(1, int(days))
    today = datetime.now().strftime("%Y-%m-%d")
    window, cal_src = trading_calendar(days, today)
    pool = B.load_json(B.POOL_PATH, [])
    codes = [p["code"] for p in pool]
    log(f"▶ 历史缺口检查（最近 {days} 个交易日）")
    log(f"  标的 {len(codes)} 只，今天 {today}")

    if not window:
        r = {"checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "today": today, "mode": "check" if check else "fill",
             "window_days": days, "window": [], "calendar_source": cal_src,
             "before": {}, "by_date": {}, "recovered": {"dates": []},
             "unrecoverable_dates": [], "notes": ["交易日历为空，跳过检查"]}
        print_report(r)
        return r

    ins = inspect(pool, codes, window)
    fills = None
    if not check:
        fills = {
            "daily": fill_daily(ins["daily"], window),
            "index": fill_index(ins["index"], pool, window),
            "sse": fill_sse_shares(ins["sse_shares"], ins["sse_codes"]),
        }
    r = build_report(days, window, cal_src, ins, fills)
    print_report(r)
    if not check:
        # 报告每次启动都会重写，但「最近一次真正补回过什么」要留着：
        # 否则第二天启动（已无缺口）就把昨天的成果从页面上一并抹掉了。
        prev = B.load_json(REPORT_PATH, {})
        if r["recovered"]["dates"]:
            r["last_recovered"] = {
                "checked_at": r["checked_at"],
                "dates": r["recovered"]["dates"],
            }
        else:
            # 兼容本功能上线前的报告：那时只有 recovered，没有 last_recovered
            r["last_recovered"] = prev.get("last_recovered") or (
                {"checked_at": prev.get("checked_at"),
                 "dates": (prev.get("recovered") or {}).get("dates")}
                if (prev.get("recovered") or {}).get("dates") else None
            )
        B.save_json(REPORT_PATH, r)
        log(f"  报告已写入 {os.path.relpath(REPORT_PATH, HERE)}")
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="检查并回补最近缺失的交易日数据")
    ap.add_argument("--check", action="store_true", help="只检查，不写任何文件")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help="检查的交易日窗口")
    a = ap.parse_args(argv)
    run(days=a.days, check=a.check)
    return 0


if __name__ == "__main__":
    sys.exit(main())
