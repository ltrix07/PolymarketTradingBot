"""
execution.py — Mock Broker (Execution Layer).

Simulates Polymarket binary market mechanics:
  - Buying YES/NO tokens at a price p (0..1)
  - Tokens qty = filled_size_usd / fill_price
  - On WIN (market resolves to 1.0): payout = qty * 1.0
  - On LOSS (market resolves to 0.0): payout = 0
  - On early exit (SL/TP): payout = qty * exit_price

New in this version:
  - Market impact slippage: larger orders in thin books move price more.
  - Partial fill simulation: orders fill 85-100% of requested size.
  - Position stores sl_pct, tp_pct (ATR-dynamic), trailing_stop_price, fill_pct.

No real orders are sent anywhere.
"""

import csv
import json
import os
import random
import uuid
from datetime import datetime, timezone

STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "state.json")


def _state_path(cfg: dict) -> str:
    filename = cfg.get("simulation", {}).get("state_file", "state.json")
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "data", filename)
    )


def _default_state(cfg: dict) -> dict:
    balance = float(cfg.get("risk_management", {}).get("initial_balance_usd", 1000.0))
    return {
        "virtual_portfolio": {
            "balance_usd": balance,
            "active_position": None,
            "daily_pnl": 0.0,
            "last_update": None,
            "trading_halted_until": None,
        },
        "trade_history": [],
    }


def load_state(cfg: dict) -> dict:
    path = _state_path(cfg)
    if not os.path.isfile(path):
        state = _default_state(cfg)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        return state
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict, cfg: dict) -> None:
    path = _state_path(cfg)
    state["virtual_portfolio"]["last_update"] = datetime.now(timezone.utc).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def reset_daily_pnl_if_needed(state: dict) -> dict:
    """Reset daily_pnl to 0.0 if the UTC calendar date has changed."""
    portfolio       = state["virtual_portfolio"]
    last_update_str = portfolio.get("last_update")
    if last_update_str is None:
        return state

    last_update = datetime.fromisoformat(last_update_str)
    if last_update.tzinfo is None:
        last_update = last_update.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    if now.date() > last_update.date():
        portfolio["daily_pnl"] = 0.0
        portfolio["last_sl_timestamp"] = None
        halted_until = portfolio.get("trading_halted_until")
        if halted_until:
            halted_dt = datetime.fromisoformat(halted_until)
            if halted_dt.tzinfo is None:
                halted_dt = halted_dt.replace(tzinfo=timezone.utc)
            if now >= halted_dt:
                portfolio["trading_halted_until"] = None

    return state


# ── Market impact & partial fill helpers ──────────────────────────────────────

def _simulate_fill_price(
    base_price: float,
    filled_size_usd: float,
    book_data: dict | None,
    cfg: dict,
    is_buy: bool = True,
) -> float:
    """Compute realistic fill price including base slippage + market impact.

    Market impact formula:
        total_slippage = base_slippage + impact_factor * (size_usd / liquidity)

    For BUY:  fill_price = base_price * (1 + total_slippage)  [paying more]
    For SELL: fill_price = base_price * (1 - total_slippage)  [receiving less]

    Liquidity is estimated from visible order book volume on the relevant side.
    Falls back to $100 when book_data is not provided.
    """
    sim_cfg       = cfg.get("simulation", {})
    base_slippage = float(sim_cfg.get("slippage_simulation_pct", 0.001))
    impact_factor = float(sim_cfg.get("market_impact_factor", 0.002))

    if book_data is not None:
        # Spread-based slippage: half the current spread is a more realistic
        # baseline than the fixed 0.1% default. Polymarket spreads are 0.02–0.08.
        spread = book_data.get("best_ask", base_price) - book_data.get("best_bid", base_price)
        if base_price > 0:
            spread_slippage_pct = max(spread / 2.0, 0.0) / base_price
        else:
            spread_slippage_pct = 0.0
        base_slippage = max(base_slippage, spread_slippage_pct)

        volume_key = "ask_volume" if is_buy else "bid_volume"
        raw_volume = book_data.get(volume_key, 0.0)
        liquidity = max(raw_volume * base_price, 10.0)
    else:
        fallback_liquidity = float(sim_cfg.get("liquidity_fallback_usd", 1000.0))
        liquidity = fallback_liquidity

    market_impact   = impact_factor * (filled_size_usd / liquidity)
    total_slippage  = base_slippage + market_impact

    if is_buy:
        return min(base_price * (1.0 + total_slippage), 0.99)
    else:
        return max(base_price * (1.0 - total_slippage), 0.0)


def _simulate_partial_fill(requested_size_usd: float, cfg: dict) -> float:
    """Return the actually-filled USD amount using a random fill percentage.

    Fill percentage is drawn uniformly from [partial_fill_min_pct, 1.0].
    The balance deduction equals only the filled amount.
    """
    min_fill_pct = float(
        cfg.get("simulation", {}).get("partial_fill_min_pct", 0.85)
    )
    fill_pct = random.uniform(min_fill_pct, 1.0)
    return requested_size_usd * fill_pct


# ── Core execution ────────────────────────────────────────────────────────────

def open_position(
    state: dict,
    side: str,
    entry_price: float,
    size_usd: float,
    market_id: str,
    cfg: dict,
    book_data: dict | None = None,
    sl_pct: float | None = None,
    tp_pct: float | None = None,
) -> dict:
    """Open a paper trading position with market-impact slippage and partial fill.

    sl_pct and tp_pct should be pre-computed by risk.compute_dynamic_sl_tp()
    and passed in; they are stored in the position for check_sl_tp() to use.

    Balance deduction = filled_size_usd (not requested size_usd).
    """
    from risk import compute_dynamic_sl_tp  # late import avoids circular dep

    # Partial fill: only a fraction of the requested order fills
    filled_size_usd = _simulate_partial_fill(size_usd, cfg)
    fill_pct        = filled_size_usd / size_usd

    # Market impact: larger order in thin book → worse entry price
    fill_price = _simulate_fill_price(entry_price, filled_size_usd, book_data, cfg, is_buy=True)

    qty = filled_size_usd / fill_price

    # Use pre-computed ATR-based SL/TP when provided, else fallback via config
    if sl_pct is None or tp_pct is None:
        sl_pct, tp_pct = compute_dynamic_sl_tp(None, cfg)

    portfolio = state["virtual_portfolio"]
    portfolio["balance_usd"] -= filled_size_usd
    portfolio["active_position"] = {
        "id":                    str(uuid.uuid4()),
        "side":                  side,
        "entry_price":           fill_price,
        "qty":                   qty,
        "size_usd":              filled_size_usd,
        "requested_size_usd":    size_usd,
        "fill_pct":              round(fill_pct, 4),
        "market_id":             market_id,
        "market_end_date_iso":   cfg.get("_current_market_end_date_iso", ""),
        "sl_pct":                round(sl_pct, 4),
        "tp_pct":                round(tp_pct, 4),
        "trailing_stop_price":   None,
        "timestamp":             datetime.now(timezone.utc).isoformat(),
    }
    return state


def close_position(
    state: dict,
    exit_price: float,
    result: str,
    cfg: dict,
    book_data: dict | None = None,
    skip_slippage: bool = False,
) -> dict:
    """Close the active paper trading position.

    Resolution modes:
      - result == "WIN"  : market resolved for our side → each token pays $1
      - result == "LOSS" : market resolved against us   → each token pays $0
      - result == "SL" or "TP" : early exit — sell tokens at exit_price with slippage

    skip_slippage=True bypasses _simulate_fill_price() and uses exit_price directly.
    Use this for Hard TP (Maker limit order — no slippage by definition).

    PnL = (qty * resolution_price) - filled_size_usd
    """
    portfolio = state["virtual_portfolio"]
    position  = portfolio.get("active_position")
    if position is None:
        return state

    fee_pct     = float(cfg.get("simulation", {}).get("fee_simulation_pct", 0.0))
    entry_price = position["entry_price"]
    size_usd    = position["size_usd"]
    qty         = position.get("qty", size_usd / entry_price)

    if result == "WIN":
        resolution_price = 1.0
    elif result == "LOSS":
        resolution_price = 0.0
    else:
        # SL or TP: early exit — Hard TP uses exact limit price, others apply slippage
        if skip_slippage:
            resolution_price = exit_price
        else:
            resolution_price = _simulate_fill_price(
                exit_price, size_usd, book_data, cfg, is_buy=False
            )

    gross_return = qty * resolution_price
    fee          = gross_return * fee_pct
    net_return   = gross_return - fee
    pnl          = net_return - size_usd

    portfolio["balance_usd"] += net_return
    portfolio["daily_pnl"]   += pnl

    state["trade_history"].append({
        "id":                  position["id"],
        "timestamp":           datetime.now(timezone.utc).isoformat(),
        "market_id":           position["market_id"],
        "side":                position["side"],
        "entry_price":         entry_price,
        "qty":                 round(qty, 4),
        "exit_price":          resolution_price,
        "size_usd":            size_usd,
        "fill_pct":            position.get("fill_pct", 1.0),
        "sl_pct":              position.get("sl_pct"),
        "tp_pct":              position.get("tp_pct"),
        "trailing_stop_price": position.get("trailing_stop_price"),
        "pnl":                 round(pnl, 4),
        "result":              result,
    })

    if result == "SL":
        portfolio["last_sl_timestamp"] = datetime.now(timezone.utc).isoformat()

    # Append trade to CSV log
    _append_trade_csv(state["trade_history"][-1], cfg)

    portfolio["active_position"] = None
    return state


def _append_trade_csv(trade: dict, cfg: dict) -> None:
    """Append a completed trade row to the CSV log file."""
    log_file = cfg.get("simulation", {}).get("log_file")
    if not log_file:
        return
    csv_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "data", log_file)
    )
    fields = [
        "timestamp", "side", "result", "entry_price", "exit_price",
        "size_usd", "qty", "fill_pct", "sl_pct", "tp_pct",
        "pnl", "trailing_stop_price",
    ]
    file_exists = os.path.isfile(csv_path)
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow(trade)
