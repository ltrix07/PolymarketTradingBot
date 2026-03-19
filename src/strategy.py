"""
strategy.py — Trading strategy module.

Signal generation uses a three-layer confirmation model:
  1. MACD crossover (primary signal — required)
  2. RSI filter      (blocks signal when price is at extremes)
  3. Order book imbalance confirmation (optional, from live book depth)

Signal fires when: MACD crossover AND at least one of (RSI ok OR book ok).
This reduces false signals while keeping the bot responsive.
"""

import logging
import pandas as pd
import pandas_ta as ta


# ── Primary signal: MACD crossover ────────────────────────────────────────────

def _compute_macd_signal(df: pd.DataFrame, cfg: dict) -> str | None:
    """Return 'BUY_YES', 'BUY_NO', or None based on MACD line/signal crossover."""
    params = cfg["strategy"]["parameters"]
    fast   = int(params["fast_ema"])
    slow   = int(params["slow_ema"])
    smooth = int(params["signal_smoothing"])

    macd_result = ta.macd(df["close"], fast=fast, slow=slow, signal=smooth)

    macd_col   = f"MACD_{fast}_{slow}_{smooth}"
    signal_col = f"MACDs_{fast}_{slow}_{smooth}"

    if macd_col not in macd_result.columns or signal_col not in macd_result.columns:
        return None

    macd_line   = macd_result[macd_col]
    signal_line = macd_result[signal_col]

    if macd_line.isna().iloc[-1] or signal_line.isna().iloc[-1]:
        return None
    if macd_line.isna().iloc[-2] or signal_line.isna().iloc[-2]:
        return None

    prev_macd   = macd_line.iloc[-2]
    prev_signal = signal_line.iloc[-2]
    curr_macd   = macd_line.iloc[-1]
    curr_signal = signal_line.iloc[-1]

    if prev_macd < prev_signal and curr_macd > curr_signal:
        return "BUY_YES"
    if prev_macd > prev_signal and curr_macd < curr_signal:
        return "BUY_NO"
    return None


# ── Confirmation 1: RSI filter ─────────────────────────────────────────────────

def _compute_rsi_confirmation(df: pd.DataFrame, cfg: dict, signal: str) -> bool:
    """Return True if RSI does NOT block the signal direction.

    BUY_YES is blocked when RSI >= overbought (already stretched upward).
    BUY_NO  is blocked when RSI <= oversold  (already stretched downward).
    Returns True (pass) when not enough data to compute.
    """
    rsi_cfg = cfg.get("strategy", {}).get("rsi", {})
    period     = int(rsi_cfg.get("period", 14))
    overbought = float(rsi_cfg.get("overbought", 65))
    oversold   = float(rsi_cfg.get("oversold", 35))

    if len(df) < period + 1:
        return True  # insufficient data — do not block

    try:
        rsi_series = ta.rsi(df["close"], length=period)
        if rsi_series is None or rsi_series.isna().iloc[-1]:
            return True
        current_rsi = float(rsi_series.iloc[-1])
    except Exception:
        return True

    if signal == "BUY_YES":
        return current_rsi < overbought   # pass if not overbought
    else:
        return current_rsi > oversold     # pass if not oversold


# ── Confirmation 2: Order book imbalance ──────────────────────────────────────

def _compute_book_confirmation(book_data: dict | None, cfg: dict, signal: str) -> bool:
    """Return True if order book imbalance confirms the signal direction.

    book_imbalance = bid_volume / (bid_volume + ask_volume)
      > threshold  → more buying pressure → confirms BUY_YES
      < 1-threshold → more selling pressure → confirms BUY_NO

    Returns True (pass) when book_data is unavailable.
    """
    if book_data is None:
        return True  # no depth data — do not block

    ob_cfg    = cfg.get("strategy", {}).get("order_book", {})
    threshold = float(ob_cfg.get("imbalance_threshold", 0.60))
    imbalance = book_data.get("book_imbalance", 0.5)

    if signal == "BUY_YES":
        return imbalance >= threshold
    else:
        return imbalance <= (1.0 - threshold)


# ── Public API ─────────────────────────────────────────────────────────────────

def generate_signal(
    candles: list[dict],
    cfg: dict,
    book_data: dict | None = None,
) -> str | None:
    """Generate a trading signal using MACD + RSI + order book confirmation.

    Requires:
      - MACD crossover (primary, mandatory)
      - At least one of: RSI not at extreme OR book imbalance confirms direction

    Args:
        candles:   List of OHLCV dicts (Binance 1m candles).
        cfg:       Bot configuration dict.
        book_data: Optional dict from fetch_polymarket_book_async (for depth).

    Returns 'BUY_YES', 'BUY_NO', or None.
    """
    if len(candles) < 20:
        return None

    df = pd.DataFrame(candles)
    df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    df["close"] = pd.to_numeric(df["close"])

    macd_signal = _compute_macd_signal(df, cfg)
    if macd_signal is None:
        return None

    rsi_ok  = _compute_rsi_confirmation(df, cfg, macd_signal)
    book_available = book_data is not None  # BUG FIX: track whether book fetch succeeded
    if not book_available:
        logging.warning("Order book unavailable — falling back to RSI-only confirmation")  # BUG FIX: warn on silent degradation
    book_ok = _compute_book_confirmation(book_data, cfg, macd_signal)

    # BUG FIX: when book is available require both confirmations; RSI-only only when book is absent
    if book_available:
        if not (rsi_ok and book_ok):
            return None
    else:
        if not rsi_ok:
            return None

    return macd_signal


def get_macd_state(candles: list[dict], cfg: dict) -> dict:
    """Return current MACD values for the status line display.

    Returns dict with keys: macd, signal, diff, source_len.
    All float values are None if calculation fails.
    """
    if len(candles) < 20:
        return {"macd": None, "signal": None, "diff": None, "source_len": len(candles)}

    params = cfg["strategy"]["parameters"]
    fast   = int(params["fast_ema"])
    slow   = int(params["slow_ema"])
    smooth = int(params["signal_smoothing"])

    try:
        df = pd.DataFrame(candles)
        df["close"] = pd.to_numeric(df["close"])
        result = ta.macd(df["close"], fast=fast, slow=slow, signal=smooth)
        m = result[f"MACD_{fast}_{slow}_{smooth}"].iloc[-1]
        s = result[f"MACDs_{fast}_{slow}_{smooth}"].iloc[-1]
        return {
            "macd":       round(float(m), 6) if pd.notna(m) else None,
            "signal":     round(float(s), 6) if pd.notna(s) else None,
            "diff":       round(float(m - s), 6) if pd.notna(m) and pd.notna(s) else None,
            "source_len": len(candles),
        }
    except Exception:
        return {"macd": None, "signal": None, "diff": None, "source_len": len(candles)}
