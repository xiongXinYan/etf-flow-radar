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
| Estimated shares | Quote-side market cap ÷ price | Sign is meaningful; magnitude is approximate |
| Excess return | ETF move minus tracking-index move | Strips out the beta that dominates ETF prices |

These feed a weighted score from −5 to +5, bucketed into plain-language verdicts. Thresholds live in one dictionary (`TH` in `snapshots/insight.py`) so they can be tuned without touching the logic.

## Why this differs from the usual version

1. **Active net flow is not called "the state bought."** It is a tick-rule estimate. ETF primary/secondary arbitrage produces offsetting prints that dilute or even invert the signal. It says which side of the book was more impatient, not who was on it.
2. **Share sign is treated as hard evidence, share amount as soft.** The sign of a share change (creation vs. redemption) is fairly firm; only the counterparty is unknown. So share direction confirms whether money moved, and active flow is a supporting check.
3. **Excess return is a first-class signal.** Most of an ETF's move is its index. Once beta is subtracted:
   - shares up + positive excess → accumulation that lifts price
   - shares up + negative excess → absorbing supply without lifting price, which looks far more like passive allocation
4. **Peer-relative volume.** Several ETFs on the same index all surging looks like passive or arbitrage demand. Only one surging looks like a deliberate buyer. Metric: this ETF's multiple ÷ the same-index median.
5. **A continuous score instead of a hard threshold.** A single "1.8× means they showed up" cut is fragile. Five factors are weighted into a score, then bucketed.
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
