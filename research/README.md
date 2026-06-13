# BTC predict research (backtest tooling)

Reproducible walk-forward backtests behind `core/btc_regime.py`. These scripts
fetch public free-tier data (Yahoo BTC-USD, alternative.me Fear&Greed, CFTC COT)
and need `pandas numpy matplotlib`. They are research/validation only — the bot
imports just the pure-logic `core/btc_regime.py`.

* `cascade_event_study.py` — event study proving the legacy ETF-outflow
  "cascade" rule is coincident (no forward edge: after a basket -4% session BTC
  is more likely to bounce than keep falling).
* `predict_regime_backtest.py` — builds & validates the trend+momentum regime
  model. OOS (2022-02→2026-06, 0.1% cost): Sharpe ~0.8 / MaxDD -20% vs
  buy&hold 0.40 / -67%.
