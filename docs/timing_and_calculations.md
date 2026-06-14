# Timing & Calculation Logic

A reference for *when* the bot acts and *how* each number is computed, as of
Part 3. Source of truth is the code; this doc summarizes it. File/line refs are
included so it stays verifiable.

---

## 1. Two separate clocks — don't conflate them

| What | Cadence | Driven by | Code |
|------|---------|-----------|------|
| Pull market data + recompute **regime** (z-score, vol) | every **~10 s** | `intervals.market_sec` | producer loop in `main.py`; `RegimeDetector.on_market` |
| Generate a **BUY/SELL decision** (strategy signal) | every **~60 s** | `intervals.signal_sec` | `StrategyManager.on_market` throttle |

So: **data + regime update every ~10 s, but the buy/sell decision is throttled
to ~once per 60 s per symbol.** The strategy manager is *called* every 10 s
(to keep strategies warm) but only *emits* a signal when
`now - last_signal_ts >= signal_interval` (`manager.py:53-54`).

### Event chain per MARKET tick (registration order is load-bearing)
```
MARKET (every ~10s):
    portfolio.on_market   -> mark positions to latest price
    detector.on_market    -> recompute regime (z-score / vol)
    manager.on_market     -> keep strategies warm; emit SIGNAL at most once/60s
    oms.on_market         -> track ref price (+ future limit-TTL sweep)
SIGNAL (<=once/60s):
    risk.on_signal        -> size + gate -> ORDER
ORDER:
    oms.on_order          -> broker.place -> FILL
FILL:
    portfolio.on_fill     -> update cash / position / PnL
```

---

## 2. Regime detector (z-score & volatility)

`src/part6_regime/detector.py`. Operates on a rolling deque of **order-book mid
prices**, one sample appended per MARKET tick (`detector.py:39, 48-49`).

- Window length: `regime_window = 30` **samples** (not seconds).
- Each sample is ~`market_sec` (10 s) apart.
- => **z-score window ≈ 30 × 10 s = 300 s = 5 minutes.**
- Warmup: needs 30 samples before producing any regime (~5 min cold start);
  returns `RANGING` until then (`detector.py:50-51, 45`).

> NOTE: the old `config.yaml` comment "~30s" was wrong — it read "30 bars" as
> "30 seconds". The true window is 30 bars × market_sec ≈ 300 s.

### z-score (`indicators.zscore`, `indicators.py:28-37`)
Over the last `window` samples `w` (here the 30-sample window):
```
z = (w[-1] - mean(w)) / std(w, ddof=1)
```
- `ddof=1` = sample standard deviation (divide by N-1).
- If `std == 0` -> z = 0.0. If fewer than `window` samples -> NaN.
- Interpretation: how many standard deviations the latest price sits from the
  window mean.

### realized volatility (`indicators.realized_vol`, `indicators.py:88-94`)
Std-dev of log returns over the window:
```
rets = diff(log(prices[-(window+1):]))     # window here = regime_window - 1 = 29
vol  = std(rets, ddof=1)
```
Called as `realized_vol(series, window - 1)` (`detector.py:55`).

### Classification (`detector.py:62-67`)
A slow EWMA tracks a volatility baseline so "high vol" means *unusually* volatile
right now:
```
base <- 0.9 * base + 0.1 * vol            # EWMA baseline (detector.py:58-60)

if   vol > highvol_mult * base:  regime = HIGH_VOL    # highvol_mult = 2.0
elif abs(z) >= trend_z:          regime = TRENDING    # trend_z = 1.0
else:                            regime = RANGING
```
Thresholds come from `config.yaml` (`regime.trend_z`, `regime.highvol_mult`).

---

## 3. Strategy indicators (different time base!)

Strategies use **1-minute klines**, NOT the 10 s mid-price series the regime
detector uses. Config: `data.timeframe = "1m"`, `data.kline_limit = 100`.

- **Momentum** (`suitable_regimes`: TRENDING): MA(10) vs MA(30) crossover + MACD
  (12/26/9). `indicators.moving_average`, `indicators.macd`.
- **Mean reversion** (RANGING / HIGH_VOL): RSI(14) + Bollinger(20, 2σ).
  `indicators.rsi`, `indicators.bollinger`.

Indicator formulas (`src/utils/indicators.py`):
- `moving_average(p, n)` = mean of last n values.
- `rsi(p, 14)` = Wilder's RSI on the last 15 prices: `100 - 100/(1+avg_gain/avg_loss)`.
- `bollinger(p, 20, 2)` = `(mid - 2σ, mid, mid + 2σ)`, σ = std(ddof=1).
- `macd(p, 12, 26, 9)` = `EMA12 - EMA26`, signal = EMA9 of that, hist = macd - signal.
- All return NaN on insufficient history.

Strategies are **stateless / position-unaware**: they emit BUY/SELL from
indicators only, with no knowledge of the current position or entry price.

---

## 4. Sizing, gating, and "can it buy forever?"

`src/part5_risk/risk_manager.py` turns a SIGNAL into a sized ORDER.

### Per-order sizing (`risk_manager.py:66-70`)
```
budget = max_position_value * clip(signal.magnitude, 0, 1)   # max_position_value = 200
qty    = budget / price
qty    = min(qty, max_position_qty)                           # max_position_qty = 1.0
```
These caps are **per order**, not per total position.

### Gates currently active
- **Funding cap** (BUY only, `risk_manager.py:84-92`): if `qty * price > cash`,
  shrink to affordable; if cash ~0 -> skip. This is the *only* thing that stops
  repeated buying today.
- **Drawdown halt** (`risk_manager.py:51-63`): if equity falls
  `>= max_drawdown` (20%) below its running peak -> `engine.halt()`, no more
  orders. Triggered by losses, not by buying per se.

### Consequence: yes, it can buy continuously
With a sustained BUY regime, the bot places **one order per ~60 s per symbol**,
each `<= max_position_value` notional, **accumulating with no total-position
cap**, bounded only by running out of cash or the 20% drawdown halt. There is
currently **no** "already long, stop adding", no cooldown, no TP/SL exit.

The knobs to control this exist in `config.yaml` (`order:` block) but are **not
wired yet** — they land in Part 4 (order lifecycle / inventory) and Part 5
(risk): `max_open_positions`, `cooldown_sec`, `take_profit_pct`,
`stop_loss_pct`, `trading_enabled`, etc. See section 5.

---

## 5. Order policy knobs (`config.yaml` → `order:`)

Single source of truth, read via `Config.order` (`src/core/config.py`). Edit
values and restart; no code change needed. (Full wiring: Part 4-5.)

| Key | Meaning | Default |
|-----|---------|---------|
| `type` | `market` (fill now) or `limit` (rest on book) | `market` |
| `limit_offset_bps` | LIMIT: bps inside mid (buy below / sell above) | 5 |
| `time_in_force` | LIMIT: GTC / IOC / FOK | GTC |
| `sizing` | `notional` / `base` / `pct_equity` / `allin` | notional |
| `notional_usdt` | size when sizing=notional | 100 |
| `base_qty` | size when sizing=base | 0.0001 |
| `pct_equity` | size when sizing=pct_equity | 0.10 |
| `allin_buffer_pct` | cash kept aside when sizing=allin | 0.01 |
| `take_profit_pct` | +% from entry -> close (0 off) | 0.03 |
| `stop_loss_pct` | -% from entry -> close (0 off) | 0.03 |
| `trailing_stop_pct` | trail peak by % (0 off) | 0.0 |
| `cancel_unfilled` | auto-cancel unfilled limit after ttl | true |
| `ttl_sec` | unfilled-limit lifetime (s) | 5 |
| `trading_enabled` | master kill-switch (false = log only) | true |
| `max_open_positions` | cap concurrent symbols held | 2 |
| `cooldown_sec` | min seconds between orders on same symbol | 30 |
| `min_notional_usdt` | skip dust orders below this | 10 |
| `max_slippage_bps` | MARKET: abort if fill off ref by > this | 50 |
| `daily_max_loss_pct` | halt for the day at this realized loss | 0.10 |

---

## 6. Key config values referenced here (`config/config.yaml`)

```
intervals.market_sec    = 10     # data + regime cadence (s)
intervals.signal_sec    = 60     # buy/sell decision cadence (s)
intervals.regime_window = 30     # z-score / vol window, in SAMPLES (≈300s)
data.timeframe          = 1m     # strategy indicator kline size
data.kline_limit        = 100    # klines kept per symbol
regime.trend_z          = 1.0    # |z| >= this -> TRENDING
regime.highvol_mult     = 2.0    # vol > mult*baseline -> HIGH_VOL
risk.max_position_value = 200    # per-order notional cap (USDT)
risk.max_position_qty   = 1.0    # per-order base-qty cap
risk.max_drawdown       = 0.20   # equity drawdown halt
```
