"""导出页面数据：写 data.json，并把同一份数据注入 index.html。

注入后页面不再依赖 fetch，双击 index.html 即可打开（不需要本地服务器）。
"""
from __future__ import annotations

import json
import os

import insight

ROOT = insight.metrics.ROOT
PAGE = os.path.join(ROOT, "index.html")
START = "<script>window.__DATA__="
END = ";</script>"


def build_payload():
    b = insight.enrich()
    hist = insight.metrics.load()

    timeseries = []
    for d in sorted(hist):
        s = hist[d]
        amt = sum((r.get("amount_yuan") or 0) for r in s["pool"] if r["tier"] == "focus")
        if not amt:
            continue
        mults = []
        for r in s["pool"]:
            if r["tier"] != "focus":
                continue
            avg = insight.metrics._daily_avg_amount(r)
            if avg:
                mults.append((r.get("amount_yuan") or 0) / avg)
        timeseries.append({
            "date": d,
            "amount": round(amt / 1e8, 2),
            "mult": round(sum(mults) / len(mults), 2) if mults else None,
        })

    return {
        "day": b["day"],
        "generated_at": b["generated_at"],
        "session_end": b["session_end"],
        "degraded": b["degraded"],
        "judge": b["judge"],
        "agg": {
            "amount": round(b["agg"]["amount"] / 1e8, 2),
            "avg20": round((b["agg"]["avg20"] or 0) / 1e8, 2),
            "mult": b["agg"]["mult"],
            "active_net": round((b["agg"]["active_net"] or 0) / 1e8, 2),
            "scale": round(b["agg"]["scale"], 1),
            "pct": round(b["agg"]["pct"] or 0, 2),
            "count": len(b["focus"]),
        },
        "rows": [_row(r) for r in b["rows"]],
        "share_series": b["share_series"],
        "timeseries": timeseries,
        "thresholds": b["thresholds"],
        "by_index": _by_index(b),
        "intraday": _intraday(b),
    }


def _row(r):
    m = r["m"]
    return {
        "code": r["code"],
        "name": r["name"],
        "index": r["index"],
        "tier": r["tier"],
        "price": r.get("price"),
        "pct": m.get("pct"),
        "scale": round(m["scale"], 1) if m.get("scale") else None,
        "amount": round(m["amount"] / 1e8, 2),
        "avg20": round(m["avg20"] / 1e8, 2) if m.get("avg20") else None,
        "mult": m.get("mult"),
        "active_net": round(m["active_net"] / 1e8, 2) if m.get("active_net") is not None else None,
        "shares": round(m["shares"], 1) if m.get("shares") else None,
        "share_chg": m.get("share_chg"),
        "share_chg_amt": m.get("share_chg_amt"),
        "shares_source": m.get("shares_source"),
        "amount_quality": m.get("amount_quality"),
        "excess": m.get("excess") or {},
        "peer_mult_med": m.get("peer_mult_med"),
        "peer_lift": m.get("peer_lift"),
        "peer_n": m.get("peer_n"),
        "tags": r.get("tags", []),
        "hhmm": m.get("hhmm"),
    }


def _by_index(b):
    g = {}
    for r in b["focus"]:
        m = r["m"]
        x = g.setdefault(r["index"], {"index": r["index"], "amount": 0.0,
                                      "avg20": 0.0, "net": 0.0, "n": 0})
        x["amount"] += (m["amount"] or 0) / 1e8
        x["avg20"] += (m["avg20"] or 0) / 1e8
        x["net"] += (m["active_net"] or 0) / 1e8
        x["n"] += 1
    out = []
    for x in g.values():
        x["amount"] = round(x["amount"], 2)
        x["avg20"] = round(x["avg20"], 2)
        x["net"] = round(x["net"], 2)
        x["mult"] = round(x["amount"] / x["avg20"], 2) if x["avg20"] else None
        out.append(x)
    out.sort(key=lambda v: -v["amount"])
    return out


def _intraday(b):
    hist = insight.metrics.load()
    snap = hist[b["day"]]
    buckets = {}
    day = b["day"].replace("-", "")
    for r in snap["pool"]:
        if r["tier"] != "focus":
            continue
        px = r.get("price") or 0
        for x in (r.get("m5") or []):
            if x["t"][:8] != day:
                continue
            k = x["t"][8:12]
            buckets[k] = buckets.get(k, 0.0) + x["volume_lots"] * 100 * px
    if not buckets:
        return []
    total = sum(buckets.values())
    return [{"t": k, "pct": round(v / total * 100, 2)} for k, v in sorted(buckets.items())]


def inject(html: str, blob: str) -> str:
    """把数据写进页面。已有则替换，没有则插到 </head> 之前。"""
    if START in html:
        i = html.index(START) + len(START)
        j = html.index(END, i)
        return html[:i] + blob + html[j:]
    snippet = START + blob + END + "\n"
    return html.replace("</head>", snippet + "</head>", 1)


def main():
    payload = build_payload()
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    out = os.path.join(ROOT, "data.json")
    with open(out, "w", encoding="utf-8") as f:
        f.write(blob)

    html = open(PAGE, encoding="utf-8").read()
    html = inject(html, blob)
    with open(PAGE, "w", encoding="utf-8") as f:
        f.write(html)

    ok = "window.__DATA__=" in html
    print(f"data.json {os.path.getsize(out)}B | index.html {os.path.getsize(PAGE)}B | "
          f"rows={len(payload['rows'])} | injected={ok}")


if __name__ == "__main__":
    main()
