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
| Shares | Exchange-cleared totals (SSE / SZSE); falls back to volume ÷ turnover rate, then market cap ÷ price | Sign is meaningful; magnitude is now official |
| Excess return | ETF move minus tracking-index move | Strips out the beta that dominates ETF prices |

These feed a weighted score, bucketed into plain-language verdicts. Weights are mutually exclusive (primary-market flow 40%, turnover 25%, excess return 20%, trade-direction estimate 15%); price change is a background variable and does not enter the score. Thresholds live in one dictionary (`TH` in `snapshots/insight.py`) so they can be tuned without touching the logic.

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
snapshots/
  pool.json                The 24-ETF universe
  history.json             Daily snapshots (quotes + daily bars + 5-min bars)
  index_daily.json         Index daily bars, for excess return
  fetch_snapshot.py        Collection only
  metrics.py               Signal computation
  insight.py               Scoring, tagging, thresholds
  export_data.py           Writes data.json and injects it into index.html
```

## Usage

```bash
python update.py              # full run
python update.py --check      # probe quote endpoints only, no files written
python update.py --no-fetch   # recompute from existing snapshots (after changing thresholds)
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
