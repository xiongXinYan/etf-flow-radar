# etf-flow-radar

A locally-run dashboard for tracking money flow in broad-based China A-share ETFs. It estimates whether large state-linked buyers are active, using only public quote data.

The point is not to reproduce official holdings — those are no longer disclosed — but to read fund flow from observable market data and to be explicit about how weak each signal is.

## Quick start

```bash
python update.py        # fetch -> compute -> write data.json -> inject index.html (~26s)
```

Then open `index.html`. Data is embedded in the page, so it works by double-click without a server. On Windows, `update.bat` does the same thing.

## What it tracks

24 broad-based and thematic ETFs across 10 underlying indices (CSI 300, CSI 500, CSI 1000, SSE 50, STAR 50, ChiNext, CSI A500 and others), 20 of which appear on a historical state-holder list.

Four signals, deliberately kept separate:

| Signal | What it measures | How much to trust it |
|---|---|---|
| Turnover multiple | Today's cumulative turnover vs. 20-day average, aligned to the same point in the session | Reliable, but noisy day to day |
| Active net flow | Tick-direction estimate from outer/inner volume | Direction only, and arbitrage cancels a lot of it |
| Net subscription | Σ (Δ shares × price) per ETF, then summed in money | Direction is firm; counterparty unknown |
| Excess return | ETF move minus tracking-index move | Strips out the beta that dominates ETF prices |

Each factor is normalised to [-1, 1], then weighted (primary-market flow 40%, turnover 25%, excess return 20%, trade-direction estimate 15%). Total score is therefore bounded in [-1, 1]. Price change is a background variable and does not enter the score. Thresholds live in one dictionary (`TH` in `snapshots/insight.py`) so they can be tuned without touching the logic.

## Why this differs from the usual version

1. **Active net flow is not called "the state bought."** It is a tick-rule estimate. ETF primary/secondary arbitrage produces offsetting prints that dilute or even invert the signal. It says which side of the book was more impatient, not who was on it.
2. **Share sign is treated as hard evidence, share amount as soft.** The sign of a share change (creation vs. redemption) is fairly firm; only the counterparty is unknown. So share direction confirms whether money moved, and active flow is a supporting check.
3. **Excess return is a first-class signal.** Most of an ETF's move is its index. Once beta is subtracted:
   - shares up + positive excess → accumulation that lifts price
   - shares up + negative excess → absorbing supply without lifting price, which looks far more like passive allocation
4. **Peer-relative volume.** Several ETFs on the same index all surging looks like passive or arbitrage demand. Only one surging looks like a deliberate buyer. Metric: this ETF's multiple ÷ the same-index median.
5. **A continuous score instead of a hard threshold.** A single "1.8× means they showed up" cut is fragile. Independent factors are weighted into a score, then bucketed.
6. **Same-point alignment throughout.** Intraday, the denominator is always `20-day average × that ETF's historical average fraction completed by this time`, so a half-day's turnover is never divided by a full-day average.
7. **Turnover-gap attribution.** Points at which large-cap ETFs account for today's shortfall, rather than showing one aggregate multiple and leaving the reader to guess.

## Layout

```
index.html                 Single-file dashboard, data embedded
data.json                  Same payload, for scripts and further analysis
update.py                  One-command pipeline (preferred entry point)
update.bat                 Windows wrapper for update.py
gapfill.py                 Startup gap check + historical repair
snapshots/
  pool.json                The 24-ETF universe
  history.json             Daily snapshots (quotes + daily bars + 5-min bars)
  daily/{code}.json        Daily klines, consumed by the historical flow series
  index_daily.json         Index daily bars, for excess return
  shares_official.json     Exchange-reported shares, keyed by data date
  gap_report.json          Latest gap-check report (written by gapfill.py)
  fetch_snapshot.py        Collection only
  metrics.py               Signal computation
  insight.py               Scoring, tagging, thresholds
  flow_history.py          Historical net subscription from official shares
  export_data.py           Writes data.json and injects it into index.html
```

## Usage

```bash
python update.py                     # gap repair -> fetch -> compute -> inject
python update.py --check             # probe quote endpoints only, no files written
python update.py --no-fetch          # recompute from existing snapshots (no network)
python update.py --gap-check         # report missing sessions only, writes nothing
python update.py --no-gapfill        # skip gap repair, fetch today as usual
python update.py --gap-days 60       # widen the gap-check window (default 20 sessions)
python gapfill.py --check            # standalone read-only gap check
```

Both underlying scripts still work standalone: `fetch_snapshot.py` only collects, `export_data.py` only computes and injects.

Re-running during the session overwrites the current day's snapshot and automatically falls back to same-point alignment. Run at least once per trading day — share changes, the accumulation index and the turnover series only build up across days, so with a single snapshot those panels show an explicit placeholder rather than a fabricated line.

Local preview, if you prefer a server:

```bash
python -m http.server 8080
# http://127.0.0.1:8080/index.html
```

## Known limitations

- **The constituent list is a static historical label, not current holdings.** It is derived from top-ten holder disclosures in 2025 annual reports (published March 2026). China's CSRC fund-disclosure standard No. 2, effective 2026-05-01, dropped the requirement for listed funds to publish their top ten holders; the 2026 interim and Q3 reports no longer disclose them. Public sources can no longer verify what state-linked entities hold.
- **Share figures are derived** (quote market cap ÷ price), not subscription/redemption data from fund companies. Market cap itself is a quote-side figure, not NAV-based.
- **Active net flow excludes block trades and negotiated transfers**, and is affected by arbitrage hedging and market-maker two-sided quoting.
- **Every conclusion is inference from price and volume.** It cannot prove that any institution traded on a given day.
- Data comes from third-party public endpoints and may be delayed, incomplete, or change definition without notice. Fund company announcements and exchange disclosures take precedence.
- Shanghai-listed share fields have historically not updated daily for the whole market at once, which is a data-vendor artefact rather than an absence of flows.

## A note on naming

The Chinese term 国家队 ("national team") is market slang for state-linked buying, not an official designation. This project is best understood as an analytical tool for inferring flow from public data — not a source of official positions, and not investment advice. The repository name reflects the data and the method, not the actors.

## Data conventions (verified, not assumed)

- **Turnover average uses unadjusted prices.** Tencent returns both `qfqday` (adjusted) and `day` (unadjusted). Adjusted close × unadjusted volume is an amount that never traded, so the 20-day average reads `day`.
- **Shares come from `volume ÷ turnover_rate`**, not `market cap ÷ price`. Field 45 is `volume × price`, so dividing it by price just reconstructs the volume again and underweights ~1.0 CNY share classes.
- **Pool-level totals are summed in money, not in shares.** One billion shares at NAV 4.0 is 4 billion CNY; the same count at NAV 1.3 is 1.3 billion. Summing share counts across ETFs inflates high-NAV products into fake accumulation.
- **Index klines are fetched by `fetch_snapshot.py` into `index_daily.json`.** Before this, nothing wrote that file and excess return silently used stale data.
- **5-minute bars accumulate in `snapshots/m5/`.** The upstream endpoint caps at ~320 bars (about 6.7 sessions) regardless of the requested count, so the same-point intraday curve is only trustworthy once a few weeks of snapshots have piled up.
- **Timestamps are parsed, never sliced.** Field 30 is `yyyymmddHHMMSS`; a fixed `[8:12]` slice breaks silently if the format ever changes to `YYYY-MM-DD HH:MM:SS`.

## Official share data (verified by live probing, 2026-11-10)

| Source | Endpoint | Coverage | History |
|---|---|---|---|
| SSE | `query.sse.com.cn/commonQuery.do?sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L` | 923 ETFs in one call, `TOT_VOL` in 万份 | `STAT_DATE` param works |
| SZSE | `www.szse.cn/api/report/ShowReport/data?CATALOGID=1945&TABKEY=tab1&PAGENO=n` | 772 ETFs, 10 per page / 78 pages, `dqgm` in 万份 | latest only; date params ignored |

Merged results land in `snapshots/shares_official.json`, keyed by data date. `metrics.shares_of()` prefers the official figure and only falls back to estimation when a date is missing.

Operational notes, all measured:
- SSE needs `Referer: https://www.sse.com.cn/`, otherwise it returns `System Error`.
- SZSE ignores date parameters and only serves the latest snapshot, so history must accumulate locally.
- SZSE resets the connection under fast sequential paging; 0.15 s spacing plus session rebuild on failure pulled all 78 pages reliably in ~16 s. It runs in a background thread alongside the quote fetch.
- Both exchanges resolve through direct connections only. A system proxy made SZSE page reads time out.
- Cross-check on all 24 pool ETFs: official vs estimated shares differ by at most 0.44%.

## Quote endpoint failover

On 2026-11-10 the daily-kline host `web.ifzq.gtimg.cn` started returning a Tencent WAF 501 page for every request, including plain `qfq` calls — while `qt.gtimg.cn` (realtime) and `ifzq.gtimg.cn/appstock/.../mkline` (5-min bars) kept working. So it was a host-level block, not a parameter or rate issue.

`fetch_daily()` now walks `KLINE_HOSTS = ("ifzq.gtimg.cn", "web.ifzq.gtimg.cn")`. The WAF page is detected by the `waf.tencent.com` marker and raised as `WAFBlocked` (not retried), so a blocked host fails over immediately instead of burning three retries. Verified the substitute host byte-for-byte: same close, same volume, same `day` (unadjusted) field.

`REQUEST_GAP = 0.35` seconds is inserted between ETFs. The original block was probably provoked by several back-to-back full collection runs during testing; a full run now takes ~30 s instead of ~24 s and no longer trips the WAF.

## Quote endpoint failover

Two separate failures on 2026-11-10, with different causes:

1. **Proxy.** The system proxy at `127.0.0.1:7897` dropped connections, raising `ProxyError: Unable to connect to proxy` and killing the whole run. Domestic quote and exchange endpoints never needed a proxy, so `get()` now goes direct first and only falls back to the proxy if a direct attempt fails. Verified by pointing `HTTP_PROXY`/`HTTPS_PROXY` at a dead port: full run still completed with zero degraded rows.
2. **WAF.** Tencent began returning 501 WAF pages for `fqkline` on **both** `ifzq.gtimg.cn` and `web.ifzq.gtimg.cn`, while `qt.gtimg.cn` (realtime) and the `mkline` 5-minute endpoint stayed healthy. Failover between Tencent hosts could not help, so the primary daily source is now Eastmoney (`push2his.eastmoney.com`, `fqt=0` for unadjusted), with Tencent kept as backup.

The Eastmoney source also returns traded amount directly (`f57`), so the 20-day average no longer relies on estimation. Cross-checked on 510300: real amount average vs volume × close differs by 0.19%.

`REQUEST_GAP = 0.35` seconds separates ETFs, and a detected WAF page raises `WAFBlocked` instead of being retried.

## Net subscription is Δshares × price, not Δmarket value

This is the one place where a plausible-looking formula gives a badly wrong answer.

`A_t = S_t × P_t` is *market value*, not subscription amount. Expanding the day-over-day change:

```
ΔA = (ΔS)·P_{t-1} + S_{t-1}·ΔP + (ΔS)·(ΔP)
```

The middle term is pure price movement. An ETF whose shares did not change at all, but whose price rose 1%, produced **4.45 亿元 of fake net subscription** on a representative pool — with zero money entering.

The correct calculation only lets share changes set the sign:

```
F_t = Σ_i (S_{i,t} − S_{i,t−1}) × P_{i,t}
```

`tests/test_share_flow.py` pins this down: shares flat with a 1% price rise must yield exactly zero flow, and a 10B-share increase at 3.3 yuan must yield 33B, not the 63B that the market-value method reports.

## Other fixes in this round

- **Excess return uses dividend-adjusted prices.** Unadjusted ETF price versus a price index books every dividend ex-date as underperformance. Daily klines now carry both `close` (unadjusted, for turnover) and `close_qfq` (adjusted, for excess return), and excess is computed as a wealth ratio `(1+Re)/(1+Ri)−1` rather than a return difference. The index side is still a price index — total-return indices are not in the free source — so this is labelled a *price* excess.
- **Index klines merge per index.** A single failed index no longer overwrites the whole file; each entry carries `last_date`.
- **Turnover baseline is quality-tagged.** `amount_quality` is `official` when every day came with a real traded amount, `estimated` when it fell back to volume × close, `mixed` otherwise.
- **5-minute bar completeness.** A day now needs ≥20 bars in the morning and ≥20 in the afternoon with no duplicate timestamps; a bar count alone accepted sessions that were missing an entire half-day.
- **Score is genuinely weighted.** Each factor is clamped to [-1, 1] first. Previously the ceilings were +1.6 / +1.0 / +0.8 / +0.6, so the nominal 40/25/20/15 split did not hold. `score_missing` names factors that were absent rather than silently scoring them as neutral.

## Backfilling history

A fresh install has one snapshot, which is not enough to compute a single day of net subscription. `backfill.py` fills that in from the two endpoints that can actually be queried backwards:

```bash
python backfill.py --days 120          # daily klines + last 60 sessions of SSE shares
python backfill.py --check             # probe only, writes nothing
```

| Source | Reach | Notes |
|---|---|---|
| Eastmoney klines | Thousands of sessions | Real traded amount + adjusted price, both stored |
| SSE shares (`STAT_DATE`) | Any past trading day | 923 ETFs per call, exact date match verified 10/10 |
| SZSE shares | **Latest only** | Cannot be backfilled; history must accumulate daily |

Measured result: 120 sessions of klines for all 24 ETFs, 54 sessions of SSE shares, **53 days with computable net subscription** (2026-08-20 → 2026-11-10).

**Coverage is deliberately incomplete.** Only Shanghai-listed ETFs can have backfilled shares, so historical flow covers 13 of the 20 focus ETFs. Missing Shenzhen flows are skipped, not estimated — `flow_n` and `comparable_n` record what each day actually covers, and the dashboard says so plainly.

`backfill.py` writes `snapshots/daily/`, `snapshots/index_daily.json` and `snapshots/shares_official.json`. It **does not** write `history.json`: that file records what the market looked like in real time, and splicing today's daily bars into a past session would fabricate intraday data that never existed.

## Automatic gap repair on startup

The collector only ever writes *today*. Skip a few sessions and those days disappear from `daily/`, `shares_official.json` and `history.json`, which truncates the historical net-subscription chart. `update.py` now runs `gapfill.py` **before** fetching quotes, so the current export can use whatever was just repaired.

```bash
python gapfill.py --check        # read-only report, writes nothing
python gapfill.py --days 60      # widen the window (default 20 sessions)
python update.py --gap-check     # same check via the one-command entry point
python update.py --no-gapfill    # skip the check
```

The trading calendar comes from index daily bars rather than guessed weekdays, and **today is excluded** — today is the live fetch's job. A gap check costs one kline request; a repair only touches sources that are actually missing. All writes are atomic (`tmp` + replace) and a failed repair is a warning, never a reason to abort the daily update.

| Source | Repairable | Mechanism |
|---|---|---|
| `snapshots/daily/{code}.json` | yes | Eastmoney klines merged in; the history start is deliberately not extended |
| `index_daily.json` | yes | same, merged per index |
| SSE rows in `shares_official.json` | yes | `STAT_DATE` per missing session; returned date must match the requested date |
| SZSE historical shares | **no** | only the latest snapshot is served; history must accumulate by running daily |
| `history.json` intraday snapshots | **no** | real-time and 5-minute state cannot be reconstructed from daily bars |
| `m5/` | limited | upstream keeps ~320 bars (≈6.7 sessions); older days are gone |

`snapshots/gap_report.json` records per-date gaps, what was recovered and what is not recoverable. A trimmed `data_gaps` summary is embedded in `data.json` and shown above the net-subscription chart, including the most recent successful repair. Snapshot gaps are counted **after the first snapshot** only: `backfill.py` deliberately never writes `history.json`, so the pre-snapshot period is not a lapse and is not reported as one.

Measured on 2026-11-14 after a two-session lapse: 3 sessions recovered (11-11 → 11-13), 24 kline files refreshed, 2 SSE share dates refilled, and the computable net-subscription series extended from 53 to 56 days (11-11 +2.8, 11-12 −99.8, 11-13 −82.1 亿元). Two sessions of intraday snapshots remained missing and are labelled as such rather than back-filled with fabricated data.

## First real result

53 sessions of official-share flow, Shanghai-focused:

- cumulative net subscription **+2935 billion CNY**
- 38 subscription days vs 15 redemption days
- largest single day +501.7 bn (2026-11-03), largest redemption −66.0 bn (2026-09-01)

Treat this as a calibration check, not a finding: it covers 13 of 20 ETFs and only ~2.5 months.
