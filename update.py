"""一键更新：采集 → 计算 → 写 data.json → 注入 index.html。

用法（在项目根目录执行）：
    python update.py

可选参数：
    --check          只检查行情接口连通性，不写任何文件
    --no-fetch       跳过采集，仅用已有快照重算与重新注入（改了指标逻辑时用）

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


def step(title: str, fn):
    print(f"\n▶ {title}")
    t0 = time.time()
    result = fn()
    print(f"  ✓ 完成（{time.time() - t0:.1f}s）")
    return result


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    no_fetch = "--no-fetch" in argv

    if check_only:
        fetch_snapshot.main(check_only=True)
        return 0

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
