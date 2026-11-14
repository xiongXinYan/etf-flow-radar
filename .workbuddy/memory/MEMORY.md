# etf-flow-radar 项目笔记

## 启动链路
`update.py` → `gapfill.py`（历史缺口检查/回补）→ `fetch_snapshot.main`（采集当天）→ `export_data.main`（写 data.json 并注入 index.html）。顺序不能反：先补历史，本次导出才用得上补回的日K 与官方份额。`update.bat` 透传所有参数。

## 数据边界
- 可回补：`snapshots/daily/*.json`（东财日K）、`index_daily.json`、`shares_official.json` 中的沪市份额（SSE `STAT_DATE` 精确回取）。
- 不可回补：`history.json` 盘中实时快照与 5 分钟K、深交所历史份额（只给最新一期）、m5 超过约 6.7 个交易日的部分。
- 严禁用日K 或今天的行情去伪造过去的盘中数据；快照缺口只从首份 history 快照之后算。
- 交易日历一律取自行情日K 的真实日期，不靠「工作日 ≠ 节假日」硬猜。
- 净申购口径：`Σ(Δ份额 × 当日价)`，不能用市值变化（价格涨跌会造出假流入），见 `tests/test_share_flow.py`。

## 本机环境
bash 内的 `python` 是 workbuddy 3.13，未装 requests；项目解释器是 `E:/workApp/python/python.exe`（`py -3` 亦指向它，requests 2.32.5）。

## 约定
涨红跌绿；金额默认亿元；页面数据内嵌，双击 index.html 即可打开，无需服务器。
