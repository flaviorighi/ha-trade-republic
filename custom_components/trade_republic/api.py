"""Read-only client for Trade Republic, built on pytr.

pytr handles the login, the session cookies and the websocket protocol, which follow every
change Trade Republic makes to its web app. pytr can also place orders, change savings plans
and pay out cash: ReadOnlyApi refuses every request that is not in ALLOWED_SUBSCRIPTIONS, so
none of that can be reached from this integration. Outside the websocket the integration only
reads: the account settings (to resume the session) and the portfolio chart (HISTORY_PATH).

Besides the portfolio, the client reads the timeline (dividends and interest received) and the
stock details (dividends announced or expected) and keeps them between updates.

Every public method of TradeRepublicClient blocks: call it from an executor thread.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
import hashlib
from http.cookiejar import LoadError
import logging
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlencode

from pytr.api import TradeRepublicApi, TradeRepublicError
from pytr.tickers import bond_pattern
import requests
from websockets.exceptions import WebSocketException

_LOGGER = logging.getLogger(__name__)

# The websocket subscriptions this integration may send. Orders, savings plans, price alarms
# and watchlist changes are subscriptions too, and are refused.
ALLOWED_SUBSCRIPTIONS = frozenset(
    {"compactPortfolioByType", "cash", "instrument", "ticker", "timelineTransactions", "stockDetails"}
)

# pytr wants a PIN even to resume a session, where it never sends it. The PIN is needed only
# to log in, so the integration does not keep it and this placeholder stands in for it.
_NO_PIN = "----"

READ_TIMEOUT = 15
UPDATE_TIMEOUT = 120

# The curve shown in the Trade Republic app: the securities, without cash. "max" goes back to
# the first purchase with sparse points, "1y" has one point a day; finer points replace coarser
# ones of the same hour. Other ranges answer HTTP 400 (seen on 3y and 6m in 2026).
HISTORY_PATH = "/api-gateway/portfolio-chart/v2/chart"
HISTORY_RANGES = ("max", "1y")
# The performance of each point counts from the start of its range: only this one starts at the
# first purchase, so only its points say how much was invested.
HISTORY_SINCE_START = "max"

# Timeline events that credit dividends and interest, as seen on a real account in 2026; CREDIT
# and SSP_CORPORATE_ACTION_INVOICE_CASH are the dividend events of older accounts.
DIVIDEND_EVENTS = frozenset({"SSP_CORPORATE_ACTION_CASH", "SSP_CORPORATE_ACTION_INVOICE_CASH", "CREDIT"})
INTEREST_EVENTS = frozenset({"INTEREST_PAYOUT", "INTEREST_PAYOUT_CREATED"})
FINAL_STATUSES = frozenset({"EXECUTED", "CANCELED"})
# The icon of an event about a security is its logo: logos/<ISIN>/v2.
ICON_ISIN = re.compile(r"logos/([A-Z]{2}[A-Z0-9]{9}[0-9])/")
# The whole timeline is read on the first update and once a day, which also picks up bookings
# that Trade Republic cancels later; in between only its newest pages.
TIMELINE_MAX_PAGES = 1000
TIMELINE_UPDATE_PAGES = 5
TIMELINE_FULL_READ = 24 * 3600
# Dividends announced and expected change rarely.
OUTLOOK_REFRESH = 12 * 3600


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
class HistoryPoint:
    """One point of the portfolio curve in the Trade Republic app."""

    time: datetime
    net_value: float
    # Performance in money since the first purchase, when known.
    gain: float | None


@dataclass(frozen=True, slots=True)
class Payment:
    """A dividend or an interest payment, as credited to the account."""

    time: datetime
    isin: str | None
    name: str
    amount: float
    currency: str


@dataclass(frozen=True, slots=True)
class ExpectedDividend:
    """The next dividend of a security, as announced."""

    ex_date: date
    payment_date: date
    per_share: float


@dataclass(frozen=True, slots=True)
class DividendOutlook:
    """What a security is expected to pay, per share and before taxes."""

    currency: str | None
    # The dividends of the current year, projected by Trade Republic, or of last year.
    annual_per_share: float | None
    next: ExpectedDividend | None


@dataclass(frozen=True, slots=True)
class Portfolio:
    """Cash and securities of one account, with the dividends and interest it received."""

    currency: str
    cash: float
    positions: dict[str, Position]
    # None until the timeline has been read once.
    dividends: tuple[Payment, ...] | None = None
    interest: tuple[Payment, ...] | None = None
    outlook: dict[str, DividendOutlook] = field(default_factory=dict)

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

    def received(self, payments: tuple[Payment, ...] | None, since: datetime | None = None) -> float | None:
        """The sum of the payments in the account currency, since a moment or ever."""
        if payments is None:
            return None
        return sum(p.amount for p in payments if p.currency == self.currency and (since is None or p.time >= since))

    def outlook_of(self, isin: str) -> DividendOutlook | None:
        outlook = self.outlook.get(isin)
        return outlook if outlook is not None and outlook.currency == self.currency else None

    def expected_annual_dividend(self, isin: str) -> float | None:
        """Quantity held times the yearly dividend per share, before taxes."""
        outlook = self.outlook_of(isin)
        if outlook is None or outlook.annual_per_share is None or isin not in self.positions:
            return None
        return self.positions[isin].quantity * outlook.annual_per_share

    @property
    def expected_annual_dividends(self) -> float | None:
        if not self.outlook:
            return None
        return sum(value for isin in self.positions if (value := self.expected_annual_dividend(isin)) is not None)

    def upcoming_dividends(self, today: date) -> list[tuple[Position, ExpectedDividend]]:
        """Announced dividends not paid yet, the soonest first."""
        upcoming = [
            (position, outlook.next)
            for isin, position in self.positions.items()
            if (outlook := self.outlook_of(isin)) is not None
            and outlook.next is not None
            and outlook.next.payment_date >= today
        ]
        return sorted(upcoming, key=lambda item: item[1].payment_date)


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
        # Timeline events whose status will not change, and the payments found among them.
        self._final_events: set[str] = set()
        self._dividends: dict[str, Payment] = {}
        self._interest: dict[str, Payment] = {}
        self._timeline_read_at: float | None = None
        self._outlook: dict[str, tuple[float, DividendOutlook]] = {}

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

    def fetch_history(self, currency: str) -> list[HistoryPoint]:
        """The portfolio curve of the Trade Republic app, one point per hour at most, oldest first."""
        api = self._resume()
        points: dict[datetime, HistoryPoint] = {}
        statuses = []
        try:
            for history_range in HISTORY_RANGES:
                query = urlencode({"secAccNo": api._sec_acc_no, "range": history_range, "currency": currency})
                response = api._web_request(f"{HISTORY_PATH}?{query}")
                statuses.append(f"{history_range}: HTTP {response.status_code}")
                if response.status_code in (401, 403):
                    raise TradeRepublicAuthError(f"HTTP {response.status_code}")
                if response.status_code != 200:
                    continue
                found = _history_points(response.json())
                statuses[-1] += f", {len(found)} points"
                for point in found:
                    hour = point.time.replace(minute=0, second=0, microsecond=0)
                    gain = point.gain if history_range == HISTORY_SINCE_START else None
                    if gain is None and hour in points:
                        gain = points[hour].gain
                    points[hour] = HistoryPoint(point.time, point.net_value, gain)
        except requests.RequestException as err:
            raise _request_error(err) from err
        except ValueError as err:
            raise TradeRepublicConnectionError(f"unexpected portfolio history: {err!r}") from err
        finally:
            api.save_websession()
        _LOGGER.debug("Portfolio history: %s", "; ".join(statuses))
        if not points:
            raise TradeRepublicConnectionError(f"no portfolio history ({'; '.join(statuses)})")
        history = [points[hour] for hour in sorted(points)]
        # The curve starts at zero until the first purchase.
        while history and history[0].net_value == 0:
            history.pop(0)
        return history

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
            await self._read_timeline(api)
            await self._read_outlook(api, isins)
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
        timeline_read = self._timeline_read_at is not None
        return Portfolio(
            currency=currency,
            cash=cash,
            positions=positions,
            dividends=tuple(self._dividends.values()) if timeline_read else None,
            interest=tuple(self._interest.values()) if timeline_read else None,
            outlook={isin: self._outlook[isin][1] for isin in isins if isin in self._outlook},
        )

    async def _recv(self, api: ReadOnlyApi) -> tuple[str, dict[str, Any], Any]:
        return await asyncio.wait_for(api.recv(), READ_TIMEOUT)

    async def _answer(self, api: ReadOnlyApi, sub_id: str) -> Any:
        """The answer to one subscription, which is then closed."""
        try:
            while True:
                got_id, _, payload = await self._recv(api)
                if got_id == sub_id:
                    return payload
        finally:
            await api.unsubscribe(sub_id)

    async def _read_timeline(self, api: ReadOnlyApi) -> None:
        """Collect the dividends and interest credited, from the newest page to the oldest."""
        full = self._timeline_read_at is None or time.monotonic() - self._timeline_read_at > TIMELINE_FULL_READ
        if full:
            final_events: set[str] = set()
            dividends: dict[str, Payment] = {}
            interest: dict[str, Payment] = {}
        else:
            final_events, dividends, interest = set(self._final_events), dict(self._dividends), dict(self._interest)
        after = None
        try:
            for _ in range(TIMELINE_MAX_PAGES if full else TIMELINE_UPDATE_PAGES):
                payload = await self._answer(api, await api.timeline_transactions(after))
                reached_known = False
                for event in payload.get("items", []):
                    if not full and event.get("id") in self._final_events:
                        reached_known = True
                        continue
                    _collect(event, final_events, dividends, interest)
                after = (payload.get("cursors") or {}).get("after")
                if not after or reached_known:
                    break
        except (TimeoutError, TradeRepublicError, KeyError, TypeError, ValueError, AttributeError) as err:
            # Dividends and interest wait for the next update; the portfolio is still good.
            _LOGGER.warning("Could not read the timeline: %r", err)
            return
        if full and after:
            _LOGGER.warning("The timeline is longer than %s pages: older payments are missing", TIMELINE_MAX_PAGES)
        self._final_events, self._dividends, self._interest = final_events, dividends, interest
        if full:
            self._timeline_read_at = time.monotonic()

    async def _read_outlook(self, api: ReadOnlyApi, isins: list[str]) -> None:
        now = time.monotonic()
        pending = {}
        for isin in isins:
            if isin not in self._outlook or now - self._outlook[isin][0] > OUTLOOK_REFRESH:
                pending[await api.stock_details(isin)] = isin
        while pending:
            try:
                sub_id, _, payload = await self._recv(api)
            except (TimeoutError, TradeRepublicError) as err:
                # Some securities may not answer: they are asked again at the next update.
                _LOGGER.debug("No stock details for %s: %r", ", ".join(pending.values()), err)
                break
            if (isin := pending.pop(sub_id, None)) is None:
                continue
            await api.unsubscribe(sub_id)
            try:
                self._outlook[isin] = (now, _dividend_outlook(payload, datetime.now(UTC).year))
            except (KeyError, TypeError, ValueError, AttributeError) as err:
                _LOGGER.debug("Unexpected stock details for %s: %r", isin, err)

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


def _collect(
    event: dict[str, Any], final_events: set[str], dividends: dict[str, Payment], interest: dict[str, Payment]
) -> None:
    """Remember an event of the timeline, and keep it if it credited a dividend or interest."""
    event_id = event["id"]
    status = event.get("status")
    if status in FINAL_STATUSES:
        final_events.add(event_id)
    event_type = (event.get("eventType") or "").upper()
    if event_type in DIVIDEND_EVENTS:
        target = dividends
    elif event_type in INTEREST_EVENTS:
        target = interest
    else:
        return
    amount = event.get("amount") or {}
    if status != "EXECUTED" or event.get("deleted") or not amount.get("value") or float(amount["value"]) <= 0:
        # Canceled bookings, and taxes booked under the same event types.
        target.pop(event_id, None)
        return
    isin = ICON_ISIN.search(event.get("icon") or "")
    target[event_id] = Payment(
        time=datetime.fromisoformat(event["timestamp"]),
        isin=isin.group(1) if isin else None,
        name=(event.get("title") or "").strip(),
        amount=float(amount["value"]),
        currency=amount.get("currency") or "",
    )


def _dividend_outlook(payload: dict[str, Any], year: int) -> DividendOutlook:
    """Dividends of a security from its stock details; funds and bonds have none."""
    currency = None
    expected = None
    if (raw := payload.get("expectedDividend")) and raw.get("amount") is not None and raw.get("paymentDate"):
        currency = raw.get("amountCurrency")
        expected = ExpectedDividend(
            ex_date=date.fromisoformat(raw.get("exDate") or raw["paymentDate"]),
            payment_date=date.fromisoformat(raw["paymentDate"]),
            per_share=float(raw["amount"]),
        )
    # Only this year or last year: an older amount would be a dividend that was stopped.
    years = {str(entry.get("periodStartDate", ""))[:4]: entry for entry in payload.get("aggregatedDividends") or []}
    annual = None
    entry = years.get(str(year)) or years.get(str(year - 1))
    if entry and entry.get("amount") is not None and currency in (None, entry.get("amountCurrency")):
        annual = float(entry["amount"])
        currency = entry.get("amountCurrency")
    return DividendOutlook(currency=currency, annual_per_share=annual, next=expected)


def _history_points(payload: Any) -> list[HistoryPoint]:
    """Points with a timestamp and a value, from a list or from the first list in an object."""
    if isinstance(payload, dict):
        payload = next((value for value in payload.values() if isinstance(value, list)), [])
    points = []
    for raw in payload if isinstance(payload, list) else []:
        try:
            time = datetime.fromtimestamp(int(raw["timestamp"]) / 1000, UTC)
            net_value = float(raw["netValue"])
        except (KeyError, TypeError, ValueError):
            continue
        gain = (raw.get("performance") or {}).get("absoluteValue")
        points.append(HistoryPoint(time, net_value, float(gain) if gain not in (None, "") else None))
    return points
