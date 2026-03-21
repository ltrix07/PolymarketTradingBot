"""
Risk Management module for Polymarket Paper Trading Bot.
Risk Management is absolute priority — all trading decisions pass through here.

New in this version:
  - calculate_atr()         : Average True Range from OHLCV candles.
  - normalize_atr()         : Scale BTC USD ATR → Polymarket 0-1 token price units.
  - compute_dynamic_sl_tp() : ATR-based SL/TP percentages (overrides fixed config).
  - update_trailing_stop()  : Move trailing stop price as position gains value.
  - check_sl_tp()           : Updated to check trailing stop + dynamic SL/TP from position.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd


# ── ATR computation ───────────────────────────────────────────────────────────

def calculate_atr(candles: list[dict], period: int = 14) -> float | None:
    """Calculate Average True Range from OHLCV candles.

    True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    ATR = rolling mean of True Range over `period` bars.

    Returns None when insufficient data.
    """
    if len(candles) < period + 1:
        return None
    try:
        df = pd.DataFrame(candles)
        df["high"]  = pd.to_numeric(df["high"])
        df["low"]   = pd.to_numeric(df["low"])
        df["close"] = pd.to_numeric(df["close"])
        prev_close  = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"]  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(period).mean().iloc[-1]
        return float(atr) if pd.notna(atr) else None
    except Exception:
        return None


def normalize_atr(atr_raw: float, last_close: float | None = None, cfg: dict | None = None) -> float:  # BUG FIX: accept cfg to read configurable fallback price
    """Scale BTC USD ATR to Polymarket token price units (0-1 scale).

    Uses last_close as BTC reference price when available; falls back to $90,000.
    Scaling: normalized = (atr / btc_price) * 9
      → At ATR=$100, BTC=$90k: normalized ≈ 0.01 (1% of token price)
      → At ATR=$500, BTC=$90k: normalized ≈ 0.05 (5% of token price)

    Clamped to [0.005, 0.10] to prevent extreme SL/TP values.
    """
    fallback_price = (cfg or {}).get("risk_management", {}).get("btc_price_fallback", 90_000.0)  # BUG FIX: read from config, not hardcoded
    btc_price = last_close if (last_close and last_close > 1000) else fallback_price  # BUG FIX: use config-driven fallback
    normalized = (atr_raw / btc_price) * 9.0
    return max(0.005, min(normalized, 0.10))


def compute_dynamic_sl_tp(
    atr_normalized: float | None,
    cfg: dict,
) -> tuple[float, float]:
    """Compute ATR-based SL and TP percentages for the position.

    Returns (sl_pct, tp_pct) to be stored in the position dict.
    Falls back to config fixed values when ATR is unavailable or disabled.

    Clamps:
      sl_pct ∈ [2%, 20%]
      tp_pct ∈ [4%, 50%]
    """
    risk_cfg = cfg.get("risk_management", {})
    use_atr  = risk_cfg.get("use_atr_dynamic", True)
    base_sl  = float(risk_cfg.get("stop_loss_pct",   0.07))
    base_tp  = float(risk_cfg.get("take_profit_pct", 0.10))

    if not use_atr or atr_normalized is None:
        return base_sl, base_tp

    sl_mult = float(risk_cfg.get("atr_sl_multiplier", 5.0))
    tp_mult = float(risk_cfg.get("atr_tp_multiplier", 10.0))

    sl_pct = max(0.02, min(atr_normalized * sl_mult, 0.20))
    tp_pct = max(0.04, min(atr_normalized * tp_mult, 0.50))

    return sl_pct, tp_pct


# ── Trailing stop ─────────────────────────────────────────────────────────────

def update_trailing_stop(
    position: dict,
    best_bid: float,
    best_ask: float,
    atr_normalized: float | None,
    cfg: dict,
    ws_extremums: dict | None = None,
) -> dict:
    """Move trailing stop price upward as the position gains value.

    When ws_extremums is provided, uses the peak prices observed between polls
    (highest_bid for YES, lowest_ask for NO) so the trailing stop ratchets to
    the true watermark, not just the price at poll time.

    For YES positions: peak price = highest_bid (best exit price seen).
      trailing_stop = peak - trail_dist.  Only moves UP.

    For NO positions: peak price = 1.0 - lowest_ask (best NO exit seen).
      trailing_stop = peak - trail_dist.  Only moves UP.

    Trail distance = trailing_stop_atr_multiplier * atr_normalized,
    with a fallback of 3% of entry price when ATR is unavailable.
    Clamped to [1%, 20%] of entry price.

    Returns the modified position dict.
    """
    if not cfg.get("risk_management", {}).get("trailing_stop_enabled", False):
        return position

    trail_mult  = float(cfg["risk_management"].get("trailing_stop_atr_multiplier", 3.0))
    entry_price = position.get("entry_price", 0.0)
    side        = position.get("side", "YES")

    if atr_normalized is not None:
        trail_dist = trail_mult * atr_normalized
    else:
        trail_dist = entry_price * 0.03  # fallback: 3% of entry

    trail_dist = max(0.01, min(trail_dist, 0.20))

    if side == "YES":
        current_price = ws_extremums["highest_bid"] if ws_extremums else best_bid
    else:
        current_price = 1.0 - (ws_extremums["lowest_ask"] if ws_extremums else best_ask)

    new_stop     = current_price - trail_dist
    current_stop = position.get("trailing_stop_price")

    # Ratchet: only move the stop in the favorable direction (upward)
    if current_stop is None or new_stop > current_stop:
        position["trailing_stop_price"] = round(new_stop, 4)

    return position


# ── SL / TP check ─────────────────────────────────────────────────────────────

def check_sl_tp(
    portfolio: dict,
    best_bid: float,
    best_ask: float,
    cfg: dict | None = None,
) -> Optional[str]:
    """Check whether the active position has hit Stop-Loss or Take-Profit.

    Priority order:
      1. Trailing stop (if set): if current exit price <= trailing_stop_price → SL
      2. Fixed/dynamic TP: stored in position as tp_pct (set at open time)
      3. Fixed/dynamic SL: stored in position as sl_pct (set at open time)

    Uses realistic bid/ask pricing:
      - YES position: entered at ask, exits at bid (best_bid).
      - NO  position: entered at (1 - YES_bid), exits at (1 - YES_ask).

    Returns "SL", "TP", or None.
    """
    position = portfolio.get("active_position")
    if position is None:
        return None

    entry_price = position.get("entry_price")
    if entry_price is None or entry_price <= 0:
        return None

    risk_cfg   = (cfg or {}).get("risk_management", {})
    side       = position.get("side", "YES")

    # Determine current exit price
    if side == "YES":
        exit_price = best_bid
    else:
        exit_price = 1.0 - best_ask

    change_pct = (exit_price - entry_price) / entry_price

    # 1. Trailing stop check (ratcheted price floor)
    trailing_stop = position.get("trailing_stop_price")
    if trailing_stop is not None and exit_price <= trailing_stop:
        return "SL"

    # 2/3. Dynamic SL/TP thresholds stored in position at open time
    sl_pct = float(position.get("sl_pct", risk_cfg.get("stop_loss_pct",   0.07)))
    tp_pct = float(position.get("tp_pct", risk_cfg.get("take_profit_pct", 0.10)))

    if change_pct >= tp_pct:
        return "TP"
    if change_pct <= -sl_pct:
        return "SL"

    return None


# ── Trade gate ────────────────────────────────────────────────────────────────

def should_open_trade(
    portfolio: dict,
    seconds_until_expiry: float,
    cfg: dict,
) -> bool:
    """Return True only if all conditions allow opening a new trade.

    Conditions:
    - No active position exists.
    - Trading is not halted (daily loss limit).
    - Market has sufficient time before expiry.
    - Balance is sufficient for a minimum position.
    """
    risk_cfg = cfg.get("risk_management", {})
    min_time          = risk_cfg.get("min_time_before_expiry_sec", 30)
    max_time          = risk_cfg.get("max_time_before_expiry_sec", 600)
    position_size_pct = risk_cfg.get("position_size_pct", 0.05)

    if portfolio.get("active_position") is not None:
        return False
    if is_trading_halted(portfolio):
        return False
    if seconds_until_expiry > max_time:
        return False
    if seconds_until_expiry < min_time:
        return False
    if portfolio.get("balance_usd", 0.0) * position_size_pct <= 0:
        return False

    return True


def calculate_position_size(portfolio: dict, cfg: dict) -> float:
    """Return size_usd = balance_usd * position_size_pct."""
    risk_cfg          = cfg.get("risk_management", {})
    position_size_pct = risk_cfg.get("position_size_pct", 0.05)
    return portfolio.get("balance_usd", 0.0) * position_size_pct


# ── Daily loss halt ───────────────────────────────────────────────────────────

def is_trading_halted(portfolio: dict) -> bool:
    """Return True if trading_halted_until is set and is in the future."""
    halted_until = portfolio.get("trading_halted_until")
    if halted_until is None:
        return False
    if not isinstance(halted_until, str):
        return False
    halted_until_dt = datetime.fromisoformat(halted_until)
    if halted_until_dt.tzinfo is None:
        halted_until_dt = halted_until_dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < halted_until_dt


def update_halt_if_needed(portfolio: dict, cfg: dict) -> dict:
    """Set trading_halted_until to now+24h if daily_pnl breaches the max loss."""
    risk_cfg            = cfg.get("risk_management", {})
    max_daily_loss_pct  = risk_cfg.get("max_daily_loss_pct", 0.10)
    balance_usd         = portfolio.get("balance_usd", 0.0)
    daily_pnl           = portfolio.get("daily_pnl", 0.0)
    max_daily_loss_usd  = -(max_daily_loss_pct * balance_usd)

    if daily_pnl <= max_daily_loss_usd:
        halted_until = datetime.now(timezone.utc) + timedelta(hours=24)
        portfolio["trading_halted_until"] = halted_until.isoformat()

    return portfolio
