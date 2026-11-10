"""申赎金额口径回归测试。

核心命题：净申购金额只能由「份额变化 × 价格」决定，
价格本身的涨跌不许混进资金流。

运行：python tests/test_share_flow.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(os.path.dirname(HERE), "snapshots")
sys.path.insert(0, SNAP)

import metrics  # noqa: E402


def _rec(code, date, shares_yi, price, tier="focus"):
    """构造一条快照记录。shares_yi 单位亿份，price 单位元。"""
    return {
        "code": code, "name": code, "index": "沪深300", "tier": tier,
        "date": date, "price": price,
        "amount_yuan": 1e8, "turnover_rate": None,
        # shares_of 的首选是官方份额，这里用 total_shares 走后备路径
        "total_shares": shares_yi * 1e8,
        "scale_yi": shares_yi * price,
    }


def _build(days, pool_codes=("A", "B", "C")):
    """days = [(date, {code: (shares_yi, price)})]，返回 build() 的结果。"""
    hist = {}
    for date, quotes in days:
        hist[date] = {
            "date": date, "fetched_at": date + " 15:30:00",
            "pool": [_rec(c, date, *quotes[c]) for c in pool_codes],
            "degraded": [],
        }
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False)

    old_hist, old_official = metrics.HIST_PATH, metrics._OFFICIAL
    metrics.HIST_PATH = path
    metrics._OFFICIAL = {}          # 强制走推算路径，隔离官方数据
    try:
        return metrics.build()
    finally:
        metrics.HIST_PATH, metrics._OFFICIAL = old_hist, old_official
        os.remove(path)


def test_price_move_is_not_flow():
    """份额一分不动、价格涨 1%：净申购必须是 0，而不是市值变化。"""
    r = _build([
        ("2026-01-05", {"A": (100, 3.0), "B": (50, 2.0), "C": (30, 1.5)}),
        ("2026-01-06", {"A": (100, 3.03), "B": (50, 2.02), "C": (30, 1.515)}),
    ])
    last = r["share_series"][-1]
    assert abs(last["flow"]) < 1e-9, f"价格变动被误算成资金流: {last['flow']}"
    # 市值口径确实会变，正是旧算法的错误来源
    assert last["amount"] > r["share_series"][0]["amount"]


def test_flow_uses_share_delta_only():
    """只有 A 多申购 10 亿份，价 3 元 -> 净申购 30 亿元。"""
    r = _build([
        ("2026-01-05", {"A": (100, 3.0), "B": (50, 2.0), "C": (30, 1.5)}),
        ("2026-01-06", {"A": (110, 3.0), "B": (50, 2.0), "C": (30, 1.5)}),
    ])
    assert abs(r["share_series"][-1]["flow"] - 30.0) < 1e-6


def test_mixed_price_and_share_move():
    """价格涨 10% 且份额增 10 亿份：只认份额，30 亿，不是 33 亿。"""
    r = _build([
        ("2026-01-05", {"A": (100, 3.0), "B": (50, 2.0), "C": (30, 1.5)}),
        ("2026-01-06", {"A": (110, 3.3), "B": (50, 2.0), "C": (30, 1.5)}),
    ])
    flow = r["share_series"][-1]["flow"]
    # 10 亿份 × 当日价 3.3 = 33 亿。纯价格贡献的 30 亿（100 亿份 × 0.3 元）必须被剔除。
    assert abs(flow - 33.0) < 1e-6, f"got {flow}"
    naive_market_value = 110 * 3.3 - 100 * 3.0     # 旧算法会给出 63 亿
    assert abs(naive_market_value - 63.0) < 1e-6
    assert abs(flow - naive_market_value) > 1e-6, "仍在用市值变化冒充资金流"


def test_first_day_has_no_flow():
    r = _build([("2026-01-05", {"A": (100, 3.0), "B": (50, 2.0), "C": (30, 1.5)})])
    assert r["share_series"][0]["flow"] is None


def test_redeem_is_negative():
    r = _build([
        ("2026-01-05", {"A": (100, 3.0), "B": (50, 2.0), "C": (30, 1.5)}),
        ("2026-01-06", {"A": (90, 3.0), "B": (50, 2.0), "C": (30, 1.5)}),
    ])
    assert abs(r["share_series"][-1]["flow"] + 30.0) < 1e-6


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
