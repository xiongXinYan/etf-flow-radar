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

import concurrent.futures
import json
import os
import re
import sys
import time
from datetime import datetime

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_DIR = os.path.join(ROOT, "snapshots")
POOL_PATH = os.path.join(SNAP_DIR, "pool.json")
HIST_PATH = os.path.join(SNAP_DIR, "history.json")
IDX_PATH = os.path.join(SNAP_DIR, "index_daily.json")
SHARE_PATH = os.path.join(SNAP_DIR, "shares_official.json")

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

# 日K 数据源顺序。
# 2026-11-10 实测：腾讯 web.ifzq / ifzq 两个域名的 fqkline 全部被 WAF 拦截（501），
# 只有实时 qt.gtimg 和 5 分钟 mkline 还活着。因此主源改为东方财富，
# 腾讯保留作备份。
# 东财额外红 利：直接返回成交额（字段 f57），不用「成交量×收盘价」估算，
# 20 日均额的口径因此更准。
EM_KLINE = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
KLINE_HOSTS = ("ifzq.gtimg.cn", "web.ifzq.gtimg.cn")     # 腾讯备份链路

# 每次请求间隔（秒）。连续高频采集会触发 WAF，这里主动降速。
REQUEST_GAP = 0.35

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Referer": "https://finance.sina.com.cn",
}


def sym(code: str) -> str:
    """6 位代码补市场前缀；已经带 sh/sz 的（指数）原样返回。"""
    if code[:2] in ("sh", "sz"):
        return code
    return ("sh" if code[0] in "56" else "sz") + code


class WAFBlocked(RuntimeError):
    """腾讯 WAF 返回的 501 拦截页：不是网络问题，重试和换参数都无效，只能换域名。"""


def _is_waf(resp) -> bool:
    t = (resp.text or "")[:400]
    return "waf.tencent.com" in t


# 直连会话：行情和交易所接口都是境内服务，不该经过代理。
# 系统代理（如 127.0.0.1:7897）断开时会抛出
# ProxyError: Unable to connect to proxy，把整个采集带崩。
_NO_PROXY = None


def _session(direct=True):
    global _NO_PROXY
    if _NO_PROXY is None:
        sess = requests.Session()
        sess.trust_env = not direct
        if direct:
            sess.proxies = {}
        _NO_PROXY = sess
    return _NO_PROXY


def reset_session():
    global _NO_PROXY
    if _NO_PROXY is not None:
        _NO_PROXY.close()
    _NO_PROXY = None


def get(url: str, **kw) -> requests.Response:
    """先直连；直连不通时才退回系统代理。

    国内行情接口直连通常更快，且不会被本地代理的断连影响。
    """
    last = None
    for attempt in range(3):
        for sess in (_session(direct=True), _session(direct=False)):
            try:
                r = sess.get(url, headers=HEADERS, timeout=20, **kw)
                if _is_waf(r):
                    raise WAFBlocked(f"WAF blocked {url}")
                r.raise_for_status()
                return r
            except WAFBlocked:
                raise
            except Exception as e:  # noqa: BLE001
                last = e
        time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def polite_sleep():
    time.sleep(REQUEST_GAP)


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


def em_secid(code: str) -> str:
    """东财 secid：沪市 1.，深市 0.。指数代码形如 sh000300 -> 1.000300。"""
    c = code[2:] if code[:2] in ("sh", "sz") else code
    market = "1" if code.startswith("sh") or (code[:2] not in ("sh", "sz") and c[0] == "5") else "0"
    return f"{market}.{c}"


def fetch_daily_em(code, days=60):
    """东方财富不复权日K（fqt=0 即不复权）。返回结构与腾讯版一致。"""
    h = dict(HEADERS, Referer="https://quote.eastmoney.com/")
    params = {
        "secid": em_secid(code),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "klt": "101", "fqt": "0", "end": "20500101", "lmt": str(days),
    }
    r = _session(direct=True).get(EM_KLINE, params=params, headers=h, timeout=20)
    r.raise_for_status()
    d = (r.json() or {}).get("data") or {}
    out = []
    for row in d.get("klines") or []:
        x = row.split(",")
        # date,open,close,high,low,volume(手),amount(元)
        out.append({
            "date": x[0], "open": float(x[1]), "close": float(x[2]),
            "high": float(x[3]), "low": float(x[4]),
            "volume_lots": float(x[5]),
            "amount": float(x[6]) if len(x) > 6 and x[6] else None,
        })
    if not out:
        raise RuntimeError(f"eastmoney empty for {code}")
    return out


def fetch_daily(code, days=60):
    """不复权日K。为什么必须不复权：

    腾讯同时提供 qfq（前复权）和 day（不复权）两套价格，同一天 510300 的
    qfq 收盘是 4.14、不复权是 4.135。用 qfq 收盘乘「不复权成交量」算历史成交额，
    在发生过分红/折算的标的上会系统性偏移；成交额必须用真实成交价算。
    """
    try:
        return fetch_daily_em(code, days)
    except Exception as em_err:  # noqa: BLE001
        if os.environ.get("ETF_DEBUG"):
            print(f"[daily] eastmoney failed for {code}: {em_err}")
    s = sym(code)
    last = None
    for host in KLINE_HOSTS:
        u = f"https://{host}/appstock/app/fqkline/get?param={s},day,,,{days},"
        try:
            k = get(u).json()["data"][s].get("day") or []
            # [date, open, close, high, low, volume(手)]
            return [
                {"date": row[0], "open": float(row[1]), "close": float(row[2]),
                 "high": float(row[3]), "low": float(row[4]), "volume_lots": float(row[5])}
                for row in k
            ]
        except WAFBlocked as e:      # 该域名被拦截，换下一个
            last = e
            time.sleep(0.5)
            continue
    raise RuntimeError(f"daily failed for {code}: {last}")


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


# ---------- 交易所官方份额 ----------
# 实测结论（2026-11-10）：
#   SSE : commonQuery.do?sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L
#         一次返回全市场 923 只，字段 STAT_DATE / SEC_CODE / SEC_NAME / TOT_VOL(万份)
#         支持 STAT_DATE 参数回取历史；需 Referer=https://www.sse.com.cn/
#   SZSE: api/report/ShowReport/data?CATALOGID=1945&TABKEY=tab1&PAGENO=n
#         每页 10 条共 772 条 / 78 页，字段 dqgm 即总份额(万份)，日期在 metadata.subname
#         忽略日期参数（只能取最新），连续高速请求会被重置连接，须退避重试
SSE_SHARE_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_SHARE_SQL = "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L"
SZSE_SHARE_URL = "http://www.szse.cn/api/report/ShowReport/data"
SZSE_SHARE_CATALOG = "1945"

_TAG = re.compile(r"<[^>]+>")


def _direct_session():
    """交易所接口走直连（见 _session 注释）。"""
    return _session(direct=True)


def _strip(x):
    return _TAG.sub("", x or "").strip()


def fetch_sse_shares(date=None):
    """上交所 ETF 总份额 -> {code: {date, name, shares}}，shares 单位亿份。"""
    h = dict(HEADERS, Referer="https://www.sse.com.cn/")
    p = {"sqlId": SSE_SHARE_SQL}
    if date:
        p["STAT_DATE"] = date
    s = _direct_session()
    try:
        r = s.get(SSE_SHARE_URL, params=p, headers=h, timeout=25)
        r.raise_for_status()
        rows = r.json().get("result") or []
    finally:
        s.close()
    out = {}
    for x in rows:
        try:
            out[x["SEC_CODE"]] = {
                "date": x["STAT_DATE"],
                "name": _strip(x.get("SEC_NAME")),
                "shares": float(x["TOT_VOL"]) / 1e4,      # 万份 -> 亿份
            }
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_szse_shares(delay=0.15, retries=4):
    """深交所 ETF 总份额 -> {code: {date, name, shares}}，shares 单位亿份。

    只能取最新一期，且必须限速：一口气打 78 页会被重置连接。
    """
    h = dict(HEADERS, Referer="https://www.szse.cn/")
    s = _direct_session()
    out, date, page, pages = {}, None, 1, 1
    try:
        while page <= pages:
            got = False
            for attempt in range(retries):
                try:
                    r = s.get(SZSE_SHARE_URL, params={
                        "SHOWTYPE": "JSON", "CATALOGID": SZSE_SHARE_CATALOG,
                        "TABKEY": "tab1", "PAGENO": str(page), "random": "0.5",
                    }, headers=h, timeout=25)
                    if r.status_code != 200 or not r.text.lstrip().startswith("["):
                        raise ValueError(f"bad body {r.status_code}")
                    tab = r.json()[0]
                    if page == 1:
                        date = tab["metadata"].get("subname")
                        pages = int(tab["metadata"].get("pagecount") or 1)
                    for x in tab["data"]:
                        code = _strip(x.get("sys_key"))
                        shares = _strip(x.get("dqgm")).replace(",", "")
                        if code and shares:
                            out[code] = {"date": date, "name": _strip(x.get("kzjcurl")),
                                         "shares": float(shares) / 1e4}
                    got = True
                    break
                except Exception:  # noqa: BLE001
                    s.close()
                    s = _direct_session()          # 连接被重置就换一条
                    time.sleep(1.5 * (attempt + 1))
            if not got:
                raise RuntimeError(f"szse page {page} failed after {retries} tries")
            page += 1
            time.sleep(delay)
    finally:
        s.close()
    return out


def merge_official_shares(fetched_at: str, off: dict) -> dict:
    """把官方份额按「数据日期」归档到 shares_official.json。

    交易所给的是当日清算后份额；同一天重复跑覆盖即可。
    返回累积后的结构，便于调用方核对。
    """
    store = {}
    if os.path.exists(SHARE_PATH):
        try:
            store = json.load(open(SHARE_PATH, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            store = {}
    for code, v in off.items():
        store.setdefault(v["date"], {})[code] = {
            "name": v["name"], "shares": round(v["shares"], 4),
        }
    json.dump(store, open(SHARE_PATH, "w", encoding="utf-8"),
              ensure_ascii=False, sort_keys=True)
    return store


def fetch_official_shares(codes, date=None):
    """合并沪深官方份额，只保留池内代码。

    两边数据日期可能不同（收盘清算后各自更新），逐条带上实际日期，
    由调用方决定是否接受，不做静默对齐。
    """
    want = set(codes)
    out = {}
    for fn in (lambda: fetch_sse_shares(date), fetch_szse_shares):
        try:
            for k, v in fn().items():
                if k in want:
                    out[k] = v
        except Exception:  # noqa: BLE001
            continue
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
        try:
            one = fetch_daily(codes[0], 5)
            print("daily ok:", len(one), one[-1] if one else None)
        except Exception as e:  # noqa: BLE001
            print("daily FAILED:", type(e).__name__, str(e)[:160])
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
    # 深交所要翻 78 页（约 15s），与行情采集并行，避免拖慢整体
    share_bg = None
    if not check_only:
        share_bg = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        share_future = share_bg.submit(fetch_official_shares, codes)

    for p in pool:
        c = p["code"]
        rec = dict(p)
        rec["date"] = today          # 供官方份额按日期对齐
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
        polite_sleep()          # 主动降速，避免连续采集触发 WAF

    # 交易所官方份额（沪深分开采集，各自带数据日期）
    try:
        off = share_future.result() if share_bg is not None else fetch_official_shares(codes)
        if off:
            merge_official_shares(today, off)
            row["shares_source"] = {
                d: sorted(k for k, v in off.items() if v["date"] == d)
                for d in {v["date"] for v in off.values()}
            }
        else:
            row["degraded"].append("shares:empty")
    except Exception as e:  # noqa: BLE001
        row["degraded"].append(f"shares:{type(e).__name__}")
    finally:
        if share_bg is not None:
            share_bg.shutdown(wait=False)

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
