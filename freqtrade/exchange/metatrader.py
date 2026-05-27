import logging
from datetime import datetime, timezone
from typing import Any
import json
import socket
import urllib.request
import urllib.parse
from typing import Optional

from freqtrade.constants import ExchangeConfig
from freqtrade.exchange import Exchange
from freqtrade.exchange.exchange_types import FtHas
from freqtrade.enums import MarginMode, TradingMode
from freqtrade.exceptions import OperationalException
from freqtrade.util.datetime_helpers import dt_ts

try:
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover
    mt5 = None

logger = logging.getLogger(__name__)

MT5_TIMEFRAME_MAPPING = {
    "1m": getattr(mt5, "TIMEFRAME_M1", 1),
    "5m": getattr(mt5, "TIMEFRAME_M5", 5),
    "15m": getattr(mt5, "TIMEFRAME_M15", 15),
    "30m": getattr(mt5, "TIMEFRAME_M30", 30),
    "1h": getattr(mt5, "TIMEFRAME_H1", 60),
    "4h": getattr(mt5, "TIMEFRAME_H4", 240),
    "1d": getattr(mt5, "TIMEFRAME_D1", 1440),
}

MT5_DEFAULT_TIMEFRAMES = list(MT5_TIMEFRAME_MAPPING.keys())


class _MetaTraderApiAdapter:
    """Minimal MetaTrader5 wrapper compatible with Freqtrade exchange expectations."""

    def __init__(self, exchange_config: ExchangeConfig, async_mode: bool = False) -> None:
        self._exchange_config = exchange_config
        self.id = exchange_config["name"].lower()
        self.name = exchange_config["name"].lower()
        self._bridge_url: Optional[str] = exchange_config.get("bridge_url")
        self._bridge_protocol: str = exchange_config.get("bridge_protocol", "http").lower()
        # For TCP, parse host:port if given as host:port
        self._bridge_host: Optional[str] = None
        self._bridge_port: Optional[int] = None
        if self._bridge_url and self._bridge_protocol == "tcp":
            try:
                host, port = self._bridge_url.split(":")
                self._bridge_host = host
                self._bridge_port = int(port)
            except Exception:
                # allow separate host/port config keys
                self._bridge_host = exchange_config.get("bridge_host")
                self._bridge_port = int(exchange_config.get("bridge_port", 5005)) if exchange_config.get("bridge_port") else None
        self.async_mode = async_mode
        self.markets: dict[str, Any] = {}
        self.timeframes = MT5_DEFAULT_TIMEFRAMES
        self.options: dict[str, Any] = {}
        self.session = None
        self._connected = False
        self.has = {
            "fetchOHLCV": True,
            "fetchTicker": True,
            "fetchBalance": True,
            "createOrder": True,
            "cancelOrder": True,
            "fetchOrder": True,
            "fetchOpenOrders": True,
            "fetchClosedOrders": True,
            "fetchTrades": True,
            "fetchOrders": False,
        }
        self.features = {}

        # If a bridge URL is configured we can skip native MT5 initialization
        if not self._bridge_url:
            self._ensure_connected()

    def _ensure_connected(self) -> None:
        if mt5 is None:
            raise OperationalException(
                "MetaTrader5 support requires the `MetaTrader5` Python package. "
                "Install it with `pip install MetaTrader5` and ensure the MetaTrader terminal "
                "is running and accessible from this host."
            )
        if self._connected:
            return

        params: dict[str, Any] = {}
        login = self._exchange_config.get("login") or self._exchange_config.get("api_key")
        password = self._exchange_config.get("password") or self._exchange_config.get("secret")
        server = self._exchange_config.get("server")
        path = self._exchange_config.get("terminal_path")

        if login is not None:
            params["login"] = int(login)
        if password is not None:
            params["password"] = password
        if server is not None:
            params["server"] = server
        if path is not None:
            params["path"] = path

        if params:
            success = mt5.initialize(**params)
        else:
            success = mt5.initialize()

        if not success:
            err = mt5.last_error()
            raise OperationalException(
                f"Failed to initialize MetaTrader5 connection. "
                f"Error: {err.code if err else 'unknown'} {err.comment if err else ''}"
            )

        self._connected = True

    def close(self) -> None:
        if self._connected:
            mt5.shutdown()
            self._connected = False

    def load_markets(self, reload: bool = False, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._ensure_connected()
        symbols = mt5.symbols_get()
        markets: dict[str, Any] = {}
        for symbol in symbols:
            if not symbol.visible:
                continue
            pair = self._symbol_to_pair(symbol.name)
            if not pair:
                continue
            markets[pair] = {
                "symbol": pair,
                "base": pair.split("/")[0],
                "quote": pair.split("/")[1],
                "active": True,
                "precision": {
                    "amount": 0,
                    "price": getattr(symbol, "digits", 5),
                },
                "limits": {
                    "amount": {
                        "min": getattr(symbol, "volume_min", 0.01),
                        "max": getattr(symbol, "volume_max", 1000.0),
                        "step": getattr(symbol, "volume_step", 0.01),
                    },
                    "price": {
                        "min": getattr(symbol, "trade_tick_size", 0.00001),
                        "max": None,
                    },
                },
                "info": symbol,
            }
        self.markets = markets
        return markets

    def _symbol_to_pair(self, symbol: str) -> str | None:
        if "/" in symbol:
            return symbol
        if len(symbol) >= 6:
            base = symbol[:3]
            quote = symbol[3:6]
            return f"{base}/{quote}"
        return None

    def fetchOHLCV(
        self,
        symbol: str,
        timeframe: str,
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[list[Any]]:
        # prefer bridge OHLCV if configured
        if self._bridge_url and self._bridge_protocol == "http":
            try:
                url = urllib.parse.urljoin(self._bridge_url.rstrip("/") + "/", "ohlcv")
                params = {"symbol": symbol, "timeframe": timeframe, "limit": limit or 500}
                data = urllib.parse.urlencode(params)
                with urllib.request.urlopen(f"{url}?{data}") as resp:
                    return json.load(resp).get("data", [])
            except Exception:
                pass
        self._ensure_connected()
        if timeframe not in MT5_TIMEFRAME_MAPPING:
            raise OperationalException(f"Unsupported MetaTrader timeframe '{timeframe}'")

        since_dt = datetime.fromtimestamp((since or 0) / 1000, tz=timezone.utc)
        count = limit or 500
        mt_timeframe = MT5_TIMEFRAME_MAPPING[timeframe]

        rates = mt5.copy_rates_from(symbol.replace("/", ""), mt_timeframe, since_dt, count)
        return [
            [
                int(r[0] * 1000),
                float(r[1]),
                float(r[2]),
                float(r[3]),
                float(r[4]),
                float(r[5]),
            ]
            for r in rates
        ]

    def fetchTicker(self, symbol: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        # prefer bridge ticker if configured
        if self._bridge_url and self._bridge_protocol == "http":
            try:
                url = urllib.parse.urljoin(self._bridge_url.rstrip("/") + "/", "ticker")
                query = urllib.parse.urlencode({"symbol": symbol})
                with urllib.request.urlopen(f"{url}?{query}") as resp:
                    return json.load(resp)
            except Exception:
                pass
        self._ensure_connected()
        tick = mt5.symbol_info_tick(symbol.replace("/", ""))
        if tick is None:
            raise OperationalException(f"Could not fetch ticker for {symbol}")
        return {
            "symbol": symbol,
            "bid": float(tick.bid),
            "ask": float(tick.ask),
            "last": float(getattr(tick, "last", tick.bid)),
            "timestamp": int(getattr(tick, "time", dt_ts()) * 1000),
            "datetime": datetime.fromtimestamp(getattr(tick, "time", dt_ts()), tz=timezone.utc).isoformat(),
            "info": tick,
        }

    def fetchBalance(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._bridge_url and self._bridge_protocol == "http":
            try:
                url = urllib.parse.urljoin(self._bridge_url.rstrip("/") + "/", "balance")
                with urllib.request.urlopen(url) as resp:
                    return json.load(resp)
            except Exception:
                pass
        self._ensure_connected()
        account = mt5.account_info()
        if account is None:
            raise OperationalException("Could not fetch MetaTrader account information.")
        currency = getattr(account, "currency", "USD")
        return {
            "info": account,
            "total": {currency: float(getattr(account, "balance", 0.0))},
            "free": {currency: float(getattr(account, "margin_free", 0.0))},
            "used": {currency: float(getattr(account, "margin", 0.0))},
        }

    def createOrder(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # If a bridge is configured, forward the order to the bridge service
        if self._bridge_url:
            payload = {
                "symbol": symbol,
                "side": side,
                "type": order_type,
                "amount": amount,
                "price": price,
            }
            if self._bridge_protocol == "http":
                try:
                    url = urllib.parse.urljoin(self._bridge_url.rstrip("/") + "/", "order")
                    data = json.dumps(payload).encode()
                    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req) as resp:
                        return json.load(resp).get("order", {})
                except Exception as e:
                    raise OperationalException(f"Bridge HTTP order failed: {e}")
            elif self._bridge_protocol == "tcp":
                # simple TCP: send JSON and read response
                try:
                    host = self._bridge_host or self._exchange_config.get("bridge_host", "127.0.0.1")
                    port = self._bridge_port or int(self._exchange_config.get("bridge_port", 5005))
                    s = socket.create_connection((host, port), timeout=5)
                    s.sendall((json.dumps({"action": "order", **payload}) + "\n").encode())
                    resp = s.recv(8192)
                    s.close()
                    return json.loads(resp.decode())
                except Exception as e:
                    raise OperationalException(f"Bridge TCP order failed: {e}")

        # fallback to native MT5 if no bridge or bridge failed
        self._ensure_connected()
        symbol_name = symbol.replace("/", "")
        direction = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
        price = price or self.fetchTicker(symbol)["ask" if side == "buy" else "bid"]
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol_name,
            "volume": float(amount),
            "type": direction,
            "price": float(price),
            "deviation": int(self._exchange_config.get("deviation", 20)),
            "type_filling": getattr(mt5, "ORDER_FILLING_FOK", 0),
            "type_time": getattr(mt5, "ORDER_TIME_GTC", 0),
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != getattr(mt5, "TRADE_RETCODE_DONE", 10009):
            raise OperationalException(
                f"MetaTrader order send failed for {symbol}: {getattr(result, 'comment', 'unknown error')}"
            )
        return {
            "id": str(getattr(result, "order", "")),
            "symbol": symbol,
            "type": order_type,
            "side": side,
            "price": float(getattr(result, "price", price)),
            "amount": float(getattr(result, "volume", amount)),
            "status": "closed",
            "info": result,
        }

    def cancelOrder(
        self,
        order_id: str | int,
        symbol: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # If bridge is configured, forward cancel
        if self._bridge_url:
            payload = {"order_id": str(order_id)}
            if self._bridge_protocol == "http":
                try:
                    url = urllib.parse.urljoin(self._bridge_url.rstrip("/") + "/", "cancel")
                    data = json.dumps(payload).encode()
                    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req) as resp:
                        return json.load(resp)
                except Exception as e:
                    raise OperationalException(f"Bridge HTTP cancel failed: {e}")
            elif self._bridge_protocol == "tcp":
                try:
                    host = self._bridge_host or self._exchange_config.get("bridge_host", "127.0.0.1")
                    port = self._bridge_port or int(self._exchange_config.get("bridge_port", 5005))
                    s = socket.create_connection((host, port), timeout=5)
                    s.sendall((json.dumps({"action": "cancel", **payload}) + "\n").encode())
                    resp = s.recv(8192)
                    s.close()
                    return json.loads(resp.decode())
                except Exception as e:
                    raise OperationalException(f"Bridge TCP cancel failed: {e}")

        self._ensure_connected()
        request = {
            "action": getattr(mt5, "TRADE_ACTION_REMOVE", 2),
            "order": int(order_id),
        }
        if symbol:
            request["symbol"] = symbol.replace("/", "")
        result = mt5.order_send(request)
        if result is None or result.retcode != getattr(mt5, "TRADE_RETCODE_DONE", 10009):
            raise OperationalException(
                f"MetaTrader cancel order failed: {getattr(result, 'comment', 'unknown error')}"
            )
        return {"id": str(order_id), "status": "canceled", "info": result}

    def fetchOpenOrders(
        self,
        symbol: str | None = None,
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        # Prefer bridge open orders
        if self._bridge_url and self._bridge_protocol == "http":
            try:
                url = urllib.parse.urljoin(self._bridge_url.rstrip("/") + "/", "open_orders")
                query = urllib.parse.urlencode({"symbol": symbol}) if symbol else ""
                with urllib.request.urlopen(f"{url}?{query}") as resp:
                    return json.load(resp).get("open_orders", [])
            except Exception:
                pass
        self._ensure_connected()
        queries = {}
        if symbol:
            queries["symbol"] = symbol.replace("/", "")
        orders = mt5.orders_get(**queries) if hasattr(mt5, "orders_get") else []
        if not orders:
            return []
        return [self._order_to_dict(order, symbol) for order in orders]

    def fetchClosedOrders(
        self,
        symbol: str | None = None,
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_connected()
        if not hasattr(mt5, "history_orders_get"):
            return []
        end = datetime.now(timezone.utc)
        start = datetime.fromtimestamp((since or 0) / 1000, tz=timezone.utc) if since else end
        orders = mt5.history_orders_get(start, end, symbol.replace("/", "") if symbol else None)
        if not orders:
            return []
        return [self._order_to_dict(order, symbol) for order in orders]

    def fetchOrder(
        self,
        order_id: str | int,
        symbol: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        self._ensure_connected()
        if hasattr(mt5, "orders_get"):
            orders = mt5.orders_get(ticket=int(order_id))
            if orders:
                return self._order_to_dict(orders[0], symbol)
        return {"id": str(order_id), "status": "unknown", "info": None}

    def _order_to_dict(self, order: Any, symbol: str | None = None) -> dict[str, Any]:
        return {
            "id": str(getattr(order, "ticket", getattr(order, "order", None))),
            "symbol": symbol or self._symbol_to_pair(getattr(order, "symbol", "")) or "",
            "type": "market",
            "side": "buy" if getattr(order, "type", 0) in (getattr(mt5, "ORDER_TYPE_BUY", 0),) else "sell",
            "price": float(getattr(order, "price_open", 0.0)),
            "amount": float(getattr(order, "volume_initial", 0.0) or getattr(order, "volume", 0.0)),
            "status": "open" if getattr(order, "state", 0) == getattr(mt5, "ORDER_STATE_FILLED", 0) else "closed",
            "info": order,
        }


class Metatrader(Exchange):
    """MetaTrader exchange class for Freqtrade.

    This adapter provides a minimal MetaTrader5 wrapper and allows the bot to
    resolve the exchange class when `exchange.name` is set to `metatrader`.
    """

    _ft_has: FtHas = {
        "always_require_api_keys": True,
        "ohlcv_has_history": True,
        "ohlcv_partial_candle": False,
        "tickers_have_price": True,
        "tickers_have_bid_ask": True,
        "trades_has_history": True,
        "ws_enabled": False,
    }

    _supported_trading_mode_margin_pairs = [(TradingMode.SPOT, MarginMode.NONE)]

    def _init_ccxt(
        self,
        exchange_config: dict[str, Any],
        sync: bool,
        ccxt_kwargs: dict[str, Any],
    ) -> Any:
        return _MetaTraderApiAdapter(exchange_config, async_mode=not sync)

    def reload_markets(self, force: bool = False, *, load_leverage_tiers: bool = True) -> None:
        if self._last_markets_refresh != 0 and not force:
            return
        self._markets = self._api.load_markets()
        self._last_markets_refresh = dt_ts()

    def exchange_has(self, endpoint: str) -> bool:
        return self._api.has.get(endpoint, False)

    @property
    def name(self) -> str:
        return self._api.name

    @property
    def id(self) -> str:
        return self._api.id
