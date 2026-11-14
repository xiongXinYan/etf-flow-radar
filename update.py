"""一键更新：历史缺口回补 → 采集 → 计算 → 写 data.json → 注入 index.html。

用法（在项目根目录执行）：
    python update.py

可选参数：
    --check          只检查行情接口连通性，不写任何文件
    --no-fetch       跳过采集与历史回补，仅用已有快照重算（改了指标逻辑时用）
    --no-gapfill     跳过启动时的历史缺口检查（仍正常采集当天）
    --gap-check      只检查最近哪些交易日缺数据，不采集、不写任何文件
    --gap-days N     历史缺口检查窗口，默认最近 20 个交易日

启动顺序说明：先补历史，再采集当天。因为补回来的日K 与官方份额会被
历史净申购曲线直接消费；反过来则本次页面仍看不到补回的日期。
历史回补失败只会告警，不会中断当日更新。

退出码：0 成功；1 采集失败（页面不会被污染，沿用上一份数据）。
"""
from __future__ import annotations

import os
import sys
import time
import traceback

# 允许从项目根目录直接运行：python update.py
HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, "snapshots")
if SNAP not in sys.path:
    sys.path.insert(0, SNAP)

import export_data  # noqa: E402
import fetch_snapshot  # noqa: E402
import gapfill  # noqa: E402


def step(title: str, fn):
    print(f"\n▶ {title}")
    t0 = time.time()
    result = fn()
    print(f"  ✓ 完成（{time.time() - t0:.1f}s）")
    return result


def _opt_int(argv: list[str], name: str, default: int) -> int:
    """读取 `--name 20` 或 `--name=20`，没给或格式不对就用默认值。"""
    for i, a in enumerate(argv):
        raw = argv[i + 1] if a == name and i + 1 < len(argv) else None
        if raw is None and a.startswith(name + "="):
            raw = a.split("=", 1)[1]
        if raw is not None:
            try:
                return int(raw)
            except ValueError:
                return default
    return default


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    no_fetch = "--no-fetch" in argv
    gap_check_only = "--gap-check" in argv
    no_gapfill = "--no-gapfill" in argv
    gap_days = _opt_int(argv, "--gap-days", 20)

    if check_only:
        fetch_snapshot.main(check_only=True)
        return 0

    # 只体检历史：不采集、不导出、不写任何文件
    if gap_check_only:
        return gapfill.main(["--check", "--days", str(gap_days)])

    # 隔了几天没跑，中间交易日会整段缺失。先补历史，再采集当天，
    # 这样本次导出的页面就能直接用到刚补回来的日K 与官方份额。
    if not (no_fetch or no_gapfill):
        try:
            step("检查历史缺口并尝试回补", lambda: gapfill.run(days=gap_days))
        except Exception as e:  # noqa: BLE001
            print(f"\n⚠ 历史回补未完成（{type(e).__name__}: {e}），跳过并继续当日采集；"
                  "已落盘的数据不受影响。")

    if no_fetch:
        print("▶ 跳过采集（--no-fetch）")
    else:
        try:
            step("采集行情快照（24 只 ETF：实时 + 日K + 5 分钟K）", fetch_snapshot.main)
        except Exception:
            print("\n✗ 采集失败，已保留上一份数据，页面不受影响。")
            traceback.print_exc()
            return 1

    try:
        step("计算指标并注入页面", export_data.main)
    except Exception:
        print("\n✗ 导出失败，data.json 与 index.html 可能未更新。")
        traceback.print_exc()
        return 1

    print("\n全部完成。打开 index.html 即可查看（双击，无需服务器）。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
