"""Reset the paper trading state to initial values."""
import json
import os

STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "state.json")

INITIAL_STATE = {
    "virtual_portfolio": {
        "balance_usd": 1000.0,
        "active_position": None,
        "daily_pnl": 0.0,
        "last_update": None,
        "trading_halted_until": None,
    },
    "trade_history": [],
}


def reset():
    path = os.path.abspath(STATE_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(INITIAL_STATE, f, indent=2)


if __name__ == "__main__":
    reset()
