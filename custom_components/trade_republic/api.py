"""Read-only client for Trade Republic, built on pytr.

pytr handles the login, the session cookies and the websocket protocol, which follow every
change Trade Republic makes to its web app. pytr can also place orders, change savings plans
and pay out cash: ReadOnlyApi refuses every request that is not in ALLOWED_SUBSCRIPTIONS, so
none of that can be reached from this integration.

Every public method of TradeRepublicClient blocks: call it from an executor thread.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
from http.cookiejar import LoadError
import logging
from pathlib import Path
from typing import Any

from pytr.api import TradeRepublicApi, TradeRepublicError
from pytr.tickers import bond_pattern
import requests
from websockets.exceptions import WebSocketException

_LOGGER = logging.getLogger(__name__)

# The websocket subscriptions this integration may send. Orders, savings plans, price alarms
# and watchlist changes are subscriptions too, and are refused.
ALLOWED_SUBSCRIPTIONS = frozenset({"compactPortfolioByType", "cash", "instrument", "ticker"})

# pytr wants a PIN even to resume a session, where it never sends it. The PIN is needed only
# to log in, so the integration does not keep it and this placeholder stands in for it.
_NO_PIN = "----"

READ_TIMEOUT = 15
UPDATE_TIMEOUT = 120


class TradeRepublicAuthError(Exception):
    """The session is gone or the login was refused: the user has to log in again."""


class TradeRepublicConnectionError(Exception):
    """Trade Republic could not be reached or did not answer as expected."""


class TooManyAttemptsError(TradeRepublicConnectionError):
    """Trade Republic asks to wait before the next login."""


class ReadOnlyError(RuntimeError):
    """Something asked pytr for a request that could change the account."""


@dataclass(frozen=True, slots=True)
class Instrument:
    """What does not change about a security: fetched once per run."""

    name: str
    exchange: str | None
    bond: bool


@dataclass(frozen=True, slots=True)
class Position:
    """One security in the portfolio."""

    isin: str
    name: str
    quantity: float
    average_price: float | None
    price: float

    @property
    def value(self) -> float:
        return self.quantity * self.price

    @property
    def invested(self) -> float | None:
        if self.average_price is None:
            return None
        return self.quantity * self.average_price

    @property
    def gain(self) -> float | None:
        if (invested := self.invested) is None:
            return None
        return self.value - invested

    @property
    def gain_percent(self) -> float | None:
        if not (invested := self.invested):
            return None
        return (self.value / invested - 1) * 100


@dataclass(frozen=True, slots=True)
class Portfolio:
    """Cash and securities of one account."""

    currency: str
    cash: float
    positions: dict[str, Position]

    @property
    def securities_value(self) -> float:
        return sum(p.value for p in self.positions.values())

    @property
    def total_value(self) -> float:
        return self.cash + self.securities_value

    @property
    def invested(self) -> float:
        return sum(p.invested for p in self.positions.values() if p.invested is not None)

    @property
    def gain(self) -> float:
        # Positions without a purchase price stay out of both sides.
        return sum(p.gain for p in self.positions.values() if p.gain is not None)

    @property
    def gain_percent(self) -> float | None:
        if not (invested := self.invested):
            return None
        return self.gain / invested * 100


class ReadOnlyApi(TradeRepublicApi):
    """pytr's client, limited to requests that only read."""

    def __init__(self, phone: str, cookies_file: Path, locale: str, pin: str | None = None) -> None:
        super().__init__(
            phone_no=phone,
            pin=pin or _NO_PIN,
            locale=locale,
            save_cookies=True,
            cookies_file=cookies_file,
            use_v2_login=True,
        )
        self._can_login = pin is not None
        # pytr logs to its own colored handler on stderr; send it to Home Assistant's log.
        self.log = _LOGGER.getChild("pytr")
        # pytr keeps these on the class: every instance would share them, and the lock would
        # stay bound to the first event loop that used it.
        self._lock = asyncio.Lock()
        self.subscriptions = {}
        self._previous_responses = {}

    def initiate_weblogin(self) -> int:
        if not self._can_login:
            raise ReadOnlyError("login without a PIN")
        return super().initiate_weblogin()

    async def subscribe(self, payload: dict[str, Any]) -> str:
        if payload.get("type") not in ALLOWED_SUBSCRIPTIONS:
            raise ReadOnlyError(f"subscription {payload.get('type')!r}")
        return await super().subscribe(payload)

    def payout(self, *args: Any, **kwargs: Any) -> Any:
        raise ReadOnlyError("payout")

    def confirm_payout(self, *args: Any, **kwargs: Any) -> Any:
        raise ReadOnlyError("payout")


def _request_error(err: requests.RequestException) -> Exception:
    status = err.response.status_code if err.response is not None else None
    if status in (401, 403):
        return TradeRepublicAuthError(f"HTTP {status}")
    if status == 429:
        return TooManyAttemptsError("HTTP 429")
    return TradeRepublicConnectionError(f"HTTP {status}" if status else str(err))


class TradeRepublicClient:
    """One Trade Republic account. Every method blocks."""

    def __init__(self, phone: str, cookies_file: Path, locale: str) -> None:
        self.phone = phone
        self._cookies_file = cookies_file
        self._locale = locale
        self._login: ReadOnlyApi | None = None
        self._instruments: dict[str, Instrument] = {}

    def start_login(self, pin: str) -> bool:
        """Send phone number and PIN.

        Returns True when Trade Republic wants a code from an authenticator app, False when
        the login has to be confirmed in the Trade Republic app.
        """
        self._cookies_file.parent.mkdir(parents=True, exist_ok=True)
        api = ReadOnlyApi(self.phone, self._cookies_file, self._locale, pin=pin)
        try:
            api.initiate_weblogin()
        except requests.RequestException as err:
            raise _request_error(err) from err
        except ValueError as err:
            raise TradeRepublicAuthError(str(err)) from err
        self._login = api
        return api.weblogin_needs_authenticator

    def wait_for_confirmation(self) -> None:
        """Wait until the login is confirmed in the app; pytr gives up after about 2 minutes."""
        self._complete_login(None)

    def complete_with_code(self, code: str) -> None:
        """Finish the login with a code from the authenticator app."""
        self._complete_login(code)

    def _complete_login(self, code: str | None) -> None:
        if self._login is None:
            raise TradeRepublicAuthError("no login in progress")
        try:
            self._login.complete_weblogin(code)
        except requests.RequestException as err:
            raise _request_error(err) from err
        except (TimeoutError, ValueError) as err:
            raise TradeRepublicAuthError(str(err)) from err
        finally:
            self._login = None

    def account_id(self) -> str:
        """A stable id for the account that does not reveal its number."""
        api = self._resume()
        try:
            number = api.settings().get("securitiesAccountNumber")
        except requests.RequestException as err:
            raise _request_error(err) from err
        if not number:
            raise TradeRepublicConnectionError("no securities account number")
        return hashlib.sha256(str(number).encode()).hexdigest()[:16]

    def forget_session(self) -> None:
        self._cookies_file.unlink(missing_ok=True)

    def fetch_portfolio(self, previous: Portfolio | None) -> Portfolio:
        """Read cash, positions and current prices."""
        api = self._resume()
        try:
            return asyncio.run(asyncio.wait_for(self._read(api, previous), UPDATE_TIMEOUT))
        except TradeRepublicError as err:
            raise TradeRepublicConnectionError(
                f"Trade Republic refused {err.subscription.get('type')}: {err.error}"
            ) from err
        except requests.RequestException as err:
            raise _request_error(err) from err
        except (OSError, TimeoutError, WebSocketException, ValueError) as err:
            raise TradeRepublicConnectionError(str(err) or type(err).__name__) from err
        finally:
            # The session may have been refreshed: keep the new cookies for the next update.
            api.save_websession()

    def _resume(self) -> ReadOnlyApi:
        if not self._cookies_file.exists():
            raise TradeRepublicAuthError("no saved session")
        api = ReadOnlyApi(self.phone, self._cookies_file, self._locale)
        try:
            api._websession.cookies.load(ignore_discard=True)
            # Refreshes the session and checks that it is still valid.
            api.settings()
        except LoadError as err:
            raise TradeRepublicAuthError("unreadable session file") from err
        except requests.RequestException as err:
            raise _request_error(err) from err
        return api

    async def _read(self, api: ReadOnlyApi, previous: Portfolio | None) -> Portfolio:
        try:
            raw_positions, raw_cash = await self._positions_and_cash(api)
            isins = [raw.get("isin") or raw["instrumentId"] for raw in raw_positions]
            await self._learn_instruments(api, isins)
            prices = await self._prices(api, isins, previous)
        finally:
            await api.close()

        try:
            positions = {}
            for isin, raw in zip(isins, raw_positions, strict=True):
                average = raw.get("averageBuyIn")
                positions[isin] = Position(
                    isin=isin,
                    name=self._instruments[isin].name,
                    quantity=float(raw["netSize"]),
                    average_price=float(average) if average not in (None, "") else None,
                    price=prices[isin],
                )
            accounts = [a for a in raw_cash if isinstance(a, dict)]
            currency = accounts[0]["currencyId"] if accounts else "EUR"
            cash = sum(float(a["amount"]) for a in accounts if a.get("currencyId") == currency)
        except (KeyError, TypeError, ValueError) as err:
            raise TradeRepublicConnectionError(f"unexpected answer from Trade Republic: {err!r}") from err
        return Portfolio(currency=currency, cash=cash, positions=positions)

    async def _recv(self, api: ReadOnlyApi) -> tuple[str, dict[str, Any], Any]:
        return await asyncio.wait_for(api.recv(), READ_TIMEOUT)

    async def _positions_and_cash(self, api: ReadOnlyApi) -> tuple[list[dict], list]:
        await api.compact_portfolio()
        await api.cash()
        positions: list[dict] | None = None
        cash: list | None = None
        while positions is None or cash is None:
            sub_id, subscription, payload = await self._recv(api)
            await api.unsubscribe(sub_id)
            if subscription["type"] == "compactPortfolioByType":
                positions = [pos for category in payload.get("categories", []) for pos in category.get("positions", [])]
            elif subscription["type"] == "cash":
                cash = payload if isinstance(payload, list) else []
        return positions, cash

    async def _learn_instruments(self, api: ReadOnlyApi, isins: list[str]) -> None:
        pending = {}
        for isin in isins:
            if isin not in self._instruments:
                pending[await api.instrument_details(isin)] = isin
        while pending:
            sub_id, _, payload = await self._recv(api)
            if (isin := pending.pop(sub_id, None)) is None:
                continue
            await api.unsubscribe(sub_id)
            name = (payload.get("shortName") or payload.get("name") or isin).strip()
            exchanges = payload.get("exchangeIds") or []
            self._instruments[isin] = Instrument(
                name=name,
                exchange=exchanges[0] if exchanges else None,
                # Bonds are quoted in percent of their face value.
                bond=payload.get("typeId") == "bond" or bool(bond_pattern.search(name)),
            )

    async def _prices(self, api: ReadOnlyApi, isins: list[str], previous: Portfolio | None) -> dict[str, float]:
        pending = {}
        for isin in isins:
            if exchange := self._instruments[isin].exchange:
                pending[await api.ticker(isin, exchange=exchange)] = isin
        prices: dict[str, float] = {}
        while pending:
            try:
                sub_id, _, payload = await self._recv(api)
            except TimeoutError:
                break
            if (isin := pending.pop(sub_id, None)) is None:
                continue
            await api.unsubscribe(sub_id)
            if (price := (payload.get("last") or {}).get("price")) is not None:
                prices[isin] = float(price) / (100 if self._instruments[isin].bond else 1)

        for isin in isins:
            if isin in prices:
                continue
            # A quote that does not come keeps the last known price rather than dropping the
            # position, which would make the totals jump.
            if previous is not None and isin in previous.positions:
                _LOGGER.debug("No price for %s, keeping the last one", isin)
                prices[isin] = previous.positions[isin].price
            else:
                raise TradeRepublicConnectionError(f"no price for {isin}")
        return prices
