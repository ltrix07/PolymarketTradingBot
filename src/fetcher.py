"""
fetcher.py — Async REST API data-fetching module for the Polymarket Paper Trading Bot.

All public functions are async (httpx.AsyncClient). Sync wrappers are provided
for backward compatibility with scripts that cannot use await.

Exposes:
  - find_active_market_id_async   : Discovery — finds current 5-min BTC market.
  - fetch_binance_klines_async    : Binance OHLCV candles.
  - fetch_polymarket_book_async   : Polymarket CLOB order book with depth metrics.
  - fetch_polymarket_history_async: Polymarket CLOB price history.
  - fetch_last_trade_price_async  : Last trade price for resolution check.

Every request uses a strict 5-second timeout.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import re

import httpx

_log = logging.getLogger(__name__)


def _parse_clob_token_ids(raw_value) -> list:
    """Parse clobTokenIds from the Gamma API response.

    The Gamma API OpenAPI schema types this field as 'string' — Polymarket stores
    it as a JSON-encoded array e.g. '["123...", "456..."]'.
    Handle both the raw-string form and the already-parsed list form defensively.
    Returns an empty list if parsing fails or the value is absent.
    """
    if not raw_value:
        return []
    if isinstance(raw_value, list):
        return [str(t) for t in raw_value if t]
    if isinstance(raw_value, str):
        stripped = raw_value.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                return [str(t) for t in parsed if t]
            except (json.JSONDecodeError, TypeError):
                pass
        if stripped:
            return [stripped]
    return []


async def find_active_market_id_async(cfg: dict, skip_token_ids: set | None = None) -> dict:
    """Discover the current active 5-minute BTC market via Gamma API (async).

    Qualification requires at least one of several signals:
      - slug_match    : slug contains "up-or-down" or "updown"
      - group_5min    : groupItemTitle contains "5 minute" or "5min"
      - desc_5min     : description contains "5-minute" or "5 minute"
      - question_5min : question contains "5" and ("minute" or " min")

    Tries multiple search queries in order of specificity; stops as soon as any
    query returns at least one qualifying candidate.

    Returns:
        Dict with keys: token_id, end_date_iso, slug
    Raises:
        httpx.TimeoutException / httpx.HTTPStatusError / ValueError
    """
    discovery_cfg = cfg.get("discovery", {})
    gamma_api = discovery_cfg.get("gamma_api", "https://gamma-api.polymarket.com")
    user_query = discovery_cfg.get("query", "Bitcoin Up or Down")
    limit = int(discovery_cfg.get("limit", 50))
    min_expiry_sec = cfg.get("risk_management", {}).get("min_time_before_expiry_sec", 30)
    coin_prefix = discovery_cfg.get("coin_slug_prefix", "btc-").lower()

    search_queries = list(dict.fromkeys([
        "Bitcoin 5",
        "BTC 5",
        "Bitcoin Up or Down",
        user_query
    ]))

    url = f"{gamma_api}/markets"
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # Обязательно возвращаем 5 страниц! (Пауза sleep(0.3), которую мы добавили ранее, спасет от бана)
    max_pages = 5  
    candidates = []
    last_query = user_query

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://polymarket.com",
        "Referer": "https://polymarket.com/"
    }
    
    proxy_url = cfg.get("endpoints", {}).get("proxy")
    async with httpx.AsyncClient(timeout=5.0, headers=headers, proxy=proxy_url) as client:
        for query in search_queries:
            base_params = {
                "active": "true",
                "closed": "false",
                "query": query,
                "limit": limit,
                "order": "endDate",
                "ascending": "true",
                "end_date_min": now_iso,
            }
            last_query = query

            for page in range(max_pages):
                # ДОБАВЛЕНО: Микро-пауза, чтобы Cloudflare не банил за Rate Limit
                if page > 0:
                    await asyncio.sleep(0.3) 

                params = {**base_params, "offset": page * limit}
                response = await client.get(url, params=params)
                response.raise_for_status()

                data = response.json()
                markets = data if isinstance(data, list) else data.get("markets", [])

                if not markets:
                    break

                for market in markets:
                    slug_raw = market.get("slug") or ""
                    group_item_title_raw = market.get("groupItemTitle") or ""
                    slug = slug_raw.lower()
                    group_item_title = group_item_title_raw.lower()
                    description = (market.get("description") or "").lower()
                    question = (market.get("question") or "").lower()

                    slug_match = "up-or-down" in slug or "updown" in slug
                    group_5min = "5 minute" in group_item_title or "5min" in group_item_title
                    desc_5min = "5-minute" in description or "5 minute" in description
                    question_5min = "5" in question and ("minute" in question or " min" in question)

                    if coin_prefix and not slug.startswith(coin_prefix):
                        continue

                    if not (slug_match or group_5min or desc_5min or question_5min):
                        continue

                    clob_ids = _parse_clob_token_ids(market.get("clobTokenIds"))
                    if not clob_ids:
                        continue
                    if skip_token_ids and clob_ids[0] in skip_token_ids:
                        continue

                    end_date_str = market.get("endDateIso") or market.get("endDate") or ""
                    end_dt = None

                    if end_date_str:
                        try:
                            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                            if end_dt.tzinfo is None:
                                end_dt = end_dt.replace(tzinfo=timezone.utc)
                        except ValueError:
                            pass

                    # Fallback: extract real expiry from Unix timestamp in slug
                    # (e.g. "btc-updown-5m-1773869400")
                    # Gamma API endDateIso may be date-only ("2026-03-19"), parsed as
                    # midnight UTC. Trigger fallback whenever end_dt is in the past.
                    if end_dt is None or end_dt <= now:
                        m = re.search(r"-(\d{9,11})$", slug_raw)
                        if m:
                            try:
                                slug_end_dt = datetime.fromtimestamp(
                                    int(m.group(1)), tz=timezone.utc
                                )
                                if slug_end_dt > now - timedelta(hours=1):
                                    end_dt = slug_end_dt
                            except (ValueError, OSError):
                                pass

                    if end_dt is None:
                        continue

                    seconds_left = (end_dt - now).total_seconds()
                    if seconds_left < min_expiry_sec:
                        continue

                    candidates.append({
                        "token_id": clob_ids[0],
                        "end_date_iso": end_dt.isoformat(),
                        "seconds_left": seconds_left,
                        "slug": slug_raw,
                    })

                if candidates:
                    break  # found on this page — no need to paginate further

            if candidates:
                break  # found with this query — skip remaining queries

    if not candidates:
        raise ValueError(
            f"No active BTC 5-min market found via Gamma API "
            f"(last query='{last_query}', pages_searched={max_pages}). "
            "Check that the market is live on Polymarket."
        )

    # ── CLOB validation ───────────────────────────────────────────────────────
    # Gamma API has stale data: it keeps marking expired/pending markets as
    # "active". Validate each candidate by probing the CLOB /book endpoint
    # before committing. Return the first candidate that responds with HTTP 200.
    clob_base = cfg["endpoints"]["polymarket_clob"]
    candidates.sort(key=lambda m: m["seconds_left"], reverse=True)

    async with httpx.AsyncClient(timeout=3.0, proxy=proxy_url) as clob_client:
        for candidate in candidates:
            try:
                r = await clob_client.get(
                    f"{clob_base}/book",
                    params={"token_id": candidate["token_id"]},
                )
                if r.status_code == 200:
                    return {
                        "token_id":    candidate["token_id"],
                        "end_date_iso": candidate["end_date_iso"],
                        "slug":        candidate["slug"],
                    }
            except Exception:
                continue

    raise ValueError(
        f"Found {len(candidates)} Gamma candidate(s) but none responded on CLOB /book "
        f"(all returned non-200). Market may be between rounds — will retry."
    )


async def fetch_binance_klines_async(cfg: dict) -> list:
    """Fetch the last 50 1-minute OHLCV candles for BTCUSDT from Binance (async)."""
    base_url = cfg["endpoints"]["binance_v3"]
    url = f"{base_url}/klines"
    params = {"symbol": "BTCUSDT", "interval": "1m", "limit": 50}

    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()

    return [
        {
            "timestamp": int(e[0]),
            "open":      float(e[1]),
            "high":      float(e[2]),
            "low":       float(e[3]),
            "close":     float(e[4]),
            "volume":    float(e[5]),
        }
        for e in response.json()
    ]


async def fetch_polymarket_book_async(cfg: dict, token_id: str) -> dict:
    """Fetch the current order-book snapshot with full depth metrics (async).

    Returns:
        best_ask, best_bid, token_id  — same as before
        bid_volume, ask_volume        — total size across top N depth levels
        book_imbalance                — bid_volume / (bid+ask) in [0,1]; >0.5 = bullish
        top_asks, top_bids            — list of {price, size} dicts
    """
    base_url = cfg["endpoints"]["polymarket_clob"]
    url = f"{base_url}/book"
    depth_levels = cfg.get("strategy", {}).get("order_book", {}).get("depth_levels", 5)
    proxy_url = cfg.get("endpoints", {}).get("proxy")

    async with httpx.AsyncClient(timeout=5.0, proxy=proxy_url) as client:
        response = await client.get(url, params={"token_id": token_id})
        response.raise_for_status()

    data = response.json()
    asks_raw = data.get("asks", [])
    bids_raw = data.get("bids", [])

    if not asks_raw:
        raise ValueError(f"Order book for token_id={token_id} has no asks.")
    if not bids_raw:
        raise ValueError(f"Order book for token_id={token_id} has no bids.")

    asks_sorted = sorted(asks_raw, key=lambda x: float(x["price"]))
    bids_sorted = sorted(bids_raw, key=lambda x: float(x["price"]), reverse=True)

    best_ask = float(asks_sorted[0]["price"])
    best_bid = float(bids_sorted[0]["price"])

    top_asks = asks_sorted[:depth_levels]
    top_bids = bids_sorted[:depth_levels]

    ask_volume = sum(float(a.get("size", 0)) for a in top_asks)
    bid_volume = sum(float(b.get("size", 0)) for b in top_bids)
    total_volume = ask_volume + bid_volume
    book_imbalance = bid_volume / total_volume if total_volume > 0 else 0.5

    return {
        "best_ask":       best_ask,
        "best_bid":       best_bid,
        "token_id":       token_id,
        "ask_volume":     ask_volume,
        "bid_volume":     bid_volume,
        "book_imbalance": book_imbalance,
        "top_asks": [{"price": float(a["price"]), "size": float(a.get("size", 0))} for a in top_asks],
        "top_bids": [{"price": float(b["price"]), "size": float(b.get("size", 0))} for b in top_bids],
    }


async def fetch_polymarket_history_async(cfg: dict, token_id: str) -> list:
    """Fetch 1-minute price history for the given market from Polymarket CLOB (async).

    interval values per API docs: max | all | 1m (month) | 1w | 1d | 6h | 1h
    fidelity = granularity in minutes (1 = 1-minute bars).
    """
    base_url = cfg["endpoints"]["polymarket_clob"]
    url = f"{base_url}/prices-history"
    params = {
        "market":   token_id,
        "interval": "1h",
        "fidelity": 1,
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()

    data = response.json()
    raw_history = data.get("history", data if isinstance(data, list) else [])

    return [
        {"timestamp": int(e["t"]), "price": float(e["p"])}
        for e in raw_history
    ]


async def fetch_last_trade_price_async(cfg: dict, token_id: str) -> float | None:
    """Fetch the last trade price for a token from Polymarket CLOB (async).

    After a market resolves, the last trade price converges to 1.0 (YES won)
    or 0.0 (NO won). Returns None if the endpoint fails or returns no data.
    """
    base_url = cfg["endpoints"]["polymarket_clob"]
    url = f"{base_url}/last-trade-price"
    proxy_url = cfg.get("endpoints", {}).get("proxy")
    try:
        async with httpx.AsyncClient(timeout=5.0, proxy=proxy_url) as client:
            response = await client.get(url, params={"token_id": token_id})
            response.raise_for_status()
        data = response.json()
        price_str = data.get("price")
        return float(price_str) if price_str not in (None, "") else None
    except Exception:
        return None


# ── Sync wrappers (backward compatibility for scripts / non-async callers) ────

def find_active_market_id(cfg: dict) -> dict:
    return asyncio.run(find_active_market_id_async(cfg))


def fetch_binance_klines(cfg: dict) -> list:
    return asyncio.run(fetch_binance_klines_async(cfg))


def fetch_polymarket_book(cfg: dict, token_id: str) -> dict:
    return asyncio.run(fetch_polymarket_book_async(cfg, token_id))


def fetch_polymarket_history(cfg: dict, token_id: str) -> list:
    return asyncio.run(fetch_polymarket_history_async(cfg, token_id))


def fetch_last_trade_price(cfg: dict, token_id: str) -> float | None:
    return asyncio.run(fetch_last_trade_price_async(cfg, token_id))


# ── Real-time WebSocket order book feed ───────────────────────────────────────

class PolymarketBookFeed:
    """WebSocket-based real-time order book feed for Polymarket CLOB.

    Maintains an up-to-date _state dict whose structure is identical to the
    dict returned by fetch_polymarket_book_async(). Use get_latest() to read
    the current snapshot from any coroutine.

    Endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market

    Handled event types:
      "book"          — full order book snapshot (rebuilds local state)
      "price_change"  — incremental delta updates
      "best_bid_ask"  — direct best-price update (no full recompute needed)

    A plaintext "PING" is sent every 10 seconds; "PONG" is silently ignored.
    On any disconnect the listener reconnects automatically after 3 seconds.
    """

    _WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    _PING_INTERVAL = 10    # seconds between keepalive PINGs
    _RECONNECT_DELAY = 3   # seconds before reconnect attempt

    def __init__(self) -> None:
        self._token_id: str | None = None
        self._cfg: dict = {}
        self._state: dict | None = None
        self._bids: dict[str, float] = {}   # price_str -> size
        self._asks: dict[str, float] = {}   # price_str -> size
        self._task: asyncio.Task | None = None
        self._running = False

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self, token_id: str, cfg: dict) -> None:
        """Connect and start the background WebSocket listener task."""
        self._token_id = token_id
        self._cfg = cfg
        self._running = True
        self._bids.clear()
        self._asks.clear()
        self._state = None
        self._task = asyncio.create_task(self._listener_loop())

    async def stop(self) -> None:
        """Cancel the background listener task and release the connection."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def get_latest(self) -> dict | None:
        """Return a shallow copy of the current _state snapshot, or None."""
        return dict(self._state) if self._state is not None else None

    # ── Internal state helpers ────────────────────────────────────────────────

    def _depth_levels(self) -> int:
        return (
            self._cfg.get("strategy", {})
            .get("order_book", {})
            .get("depth_levels", 5)
        )

    def _recompute_state(self) -> None:
        """Rebuild the full _state dict from the current bids/asks dicts.

        Uses the same depth_levels / imbalance logic as
        fetch_polymarket_book_async() so callers see an identical structure.
        """
        depth = self._depth_levels()
        asks_sorted = sorted(
            [{"price": float(p), "size": s} for p, s in self._asks.items() if s > 0],
            key=lambda x: x["price"],
        )
        bids_sorted = sorted(
            [{"price": float(p), "size": s} for p, s in self._bids.items() if s > 0],
            key=lambda x: x["price"],
            reverse=True,
        )
        if not asks_sorted or not bids_sorted:
            return

        top_asks = asks_sorted[:depth]
        top_bids = bids_sorted[:depth]
        ask_volume = sum(a["size"] for a in top_asks)
        bid_volume = sum(b["size"] for b in top_bids)
        total = ask_volume + bid_volume
        book_imbalance = bid_volume / total if total > 0 else 0.5

        self._state = {
            "best_ask":       asks_sorted[0]["price"],
            "best_bid":       bids_sorted[0]["price"],
            "token_id":       self._token_id,
            "ask_volume":     ask_volume,
            "bid_volume":     bid_volume,
            "book_imbalance": book_imbalance,
            "top_asks":       top_asks,
            "top_bids":       top_bids,
        }

    def _apply_book(self, data: dict) -> None:
        """Rebuild full book from a 'book' event (full snapshot)."""
        self._asks.clear()
        self._bids.clear()
        for entry in data.get("asks", []):
            s = float(entry.get("size", 0))
            if s > 0:
                self._asks[str(entry.get("price", "0"))] = s
        for entry in data.get("bids", []):
            s = float(entry.get("size", 0))
            if s > 0:
                self._bids[str(entry.get("price", "0"))] = s
        self._recompute_state()

    def _apply_price_change(self, data: dict) -> None:
        """Apply incremental delta updates from a 'price_change' event."""
        for change in data.get("changes", []):
            price = str(change.get("price", "0"))
            size  = float(change.get("size", 0))
            side  = change.get("side", "").lower()
            if side == "ask":
                if size == 0:
                    self._asks.pop(price, None)
                else:
                    self._asks[price] = size
            elif side == "bid":
                if size == 0:
                    self._bids.pop(price, None)
                else:
                    self._bids[price] = size
        self._recompute_state()

    def _apply_best_bid_ask(self, data: dict) -> None:
        """Directly update best_bid / best_ask from a 'best_bid_ask' event."""
        if self._state is None:
            return
        new_state = dict(self._state)
        if "best_ask" in data:
            new_state["best_ask"] = float(data["best_ask"])
        if "best_bid" in data:
            new_state["best_bid"] = float(data["best_bid"])
        self._state = new_state

    # ── Background listener ───────────────────────────────────────────────────

    async def _listener_loop(self) -> None:
        """Connect, subscribe, and dispatch incoming messages indefinitely.

        The import of websockets is deferred so that the module loads even if
        the library is not installed (the guard in main.py sets use_ws=False).
        """
        import websockets  # deferred — guarded by _WS_AVAILABLE in main.py

        while self._running:
            try:
                async with websockets.connect(self._WS_URL) as ws:
                    await ws.send(json.dumps({
                        "assets_ids": [self._token_id],
                        "type": "market",
                        "custom_feature_enabled": True,
                    }))

                    loop = asyncio.get_running_loop()
                    last_ping = loop.time()

                    while self._running:
                        elapsed      = loop.time() - last_ping
                        recv_timeout = max(0.05, self._PING_INTERVAL - elapsed)

                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                        except asyncio.TimeoutError:
                            await ws.send("PING")
                            last_ping = loop.time()
                            continue

                        if raw == "PONG":
                            continue

                        try:
                            msg = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            continue

                        event_type = msg.get("event_type") or msg.get("type", "")
                        if event_type == "book":
                            self._apply_book(msg)
                        elif event_type == "price_change":
                            self._apply_price_change(msg)
                        elif event_type == "best_bid_ask":
                            self._apply_best_bid_ask(msg)

            except asyncio.CancelledError:
                return
            except Exception as exc:
                _log.warning(
                    "PolymarketBookFeed disconnected (%s) — reconnecting in %ds",
                    exc, self._RECONNECT_DELAY,
                )
                if self._running:
                    await asyncio.sleep(self._RECONNECT_DELAY)
