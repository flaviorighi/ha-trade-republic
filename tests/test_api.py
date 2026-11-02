"""Tests for the read-only client, with pytr talking to a fake websocket."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from custom_components.trade_republic import api as api_module
from custom_components.trade_republic.api import (
    ALLOWED_SUBSCRIPTIONS,
    HistoryPoint,
    ReadOnlyApi,
    ReadOnlyError,
    TradeRepublicAuthError,
    TradeRepublicClient,
    TradeRepublicConnectionError,
)

from .conftest import PHONE

INSTRUMENTS = {
    # Trade Republic sends some names with a trailing space.
    "NL0010273215": {"shortName": "ASML ", "exchangeIds": ["LSX"], "typeId": "stock"},
    "DE0001102580": {"shortName": "Germany Feb. 2032", "exchangeIds": ["LSX"], "typeId": "bond"},
}
PRICES = {"NL0010273215": "101.5", "DE0001102580": "92.4"}


class FakeWebSocket:
    """Answers pytr's subscriptions the way Trade Republic does."""

    close_code = None

    def __init__(self, answers: dict[str, Callable[[dict], Any]]) -> None:
        self._answers = answers
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self.subscriptions: list[dict] = []

    async def send(self, message: str) -> None:
        if not message.startswith("sub "):
            return
        _, sub_id, payload = message.split(" ", 2)
        request = json.loads(payload)
        self.subscriptions.append(request)
        if (answer := self._answers[request["type"]](request)) is not None:
            self._queue.put_nowait(f"{sub_id} A {json.dumps(answer)}")

    async def recv(self) -> str:
        return await self._queue.get()

    async def close(self) -> None:
        """Nothing to close."""


def default_answers() -> dict[str, Callable[[dict], Any]]:
    return {
        "compactPortfolioByType": lambda _: {
            "categories": [
                {"positions": [{"isin": "NL0010273215", "netSize": "10", "averageBuyIn": "80"}]},
                {"positions": [{"isin": "DE0001102580", "netSize": "1000", "averageBuyIn": "0.9"}]},
            ]
        },
        "cash": lambda _: [{"accountNumber": "x", "currencyId": "EUR", "amount": 1234.5}],
        "instrument": lambda request: INSTRUMENTS[request["id"]],
        "ticker": lambda request: {"last": {"price": PRICES[request["id"].split(".")[0]]}},
    }


def make_api(tmp_path: Path, ws: FakeWebSocket) -> ReadOnlyApi:
    api = ReadOnlyApi(PHONE, tmp_path / "session.cookies", "en")
    api._sec_acc_no = "1234567890"
    api._get_ws = AsyncMock(return_value=ws)
    return api


def fetch(tmp_path: Path, client: TradeRepublicClient, ws: FakeWebSocket, previous=None):
    with patch.object(TradeRepublicClient, "_resume", return_value=make_api(tmp_path, ws)):
        return client.fetch_portfolio(previous)


def test_reads_portfolio(tmp_path: Path) -> None:
    client = TradeRepublicClient(PHONE, tmp_path / "session.cookies", "en")
    portfolio = fetch(tmp_path, client, FakeWebSocket(default_answers()))

    assert portfolio.currency == "EUR"
    assert portfolio.cash == 1234.5
    asml = portfolio.positions["NL0010273215"]
    assert asml.name == "ASML"
    assert asml.value == pytest.approx(1015.0)
    assert asml.gain == pytest.approx(215.0)
    # Bonds are quoted in percent.
    bond = portfolio.positions["DE0001102580"]
    assert bond.price == pytest.approx(0.924)
    assert bond.value == pytest.approx(924.0)
    assert portfolio.total_value == pytest.approx(1234.5 + 1015.0 + 924.0)
    assert portfolio.invested == pytest.approx(800.0 + 900.0)


def test_only_allowed_subscriptions_are_sent(tmp_path: Path) -> None:
    client = TradeRepublicClient(PHONE, tmp_path / "session.cookies", "en")
    ws = FakeWebSocket(default_answers())
    fetch(tmp_path, client, ws)
    assert {request["type"] for request in ws.subscriptions} <= ALLOWED_SUBSCRIPTIONS


def test_instruments_are_fetched_once(tmp_path: Path) -> None:
    client = TradeRepublicClient(PHONE, tmp_path / "session.cookies", "en")
    fetch(tmp_path, client, FakeWebSocket(default_answers()))
    ws = FakeWebSocket(default_answers())
    fetch(tmp_path, client, ws)
    assert "instrument" not in {request["type"] for request in ws.subscriptions}


def test_missing_price_keeps_the_last_one(tmp_path: Path) -> None:
    client = TradeRepublicClient(PHONE, tmp_path / "session.cookies", "en")
    first = fetch(tmp_path, client, FakeWebSocket(default_answers()))

    answers = default_answers()
    answers["ticker"] = lambda request: None if request["id"].startswith("DE") else {"last": {"price": "110"}}
    with patch.object(api_module, "READ_TIMEOUT", 0.1):
        second = fetch(tmp_path, client, FakeWebSocket(answers), previous=first)
        assert second.positions["NL0010273215"].price == 110.0
        assert second.positions["DE0001102580"].price == first.positions["DE0001102580"].price

        with pytest.raises(TradeRepublicConnectionError):
            fetch(tmp_path, TradeRepublicClient(PHONE, tmp_path / "other.cookies", "en"), FakeWebSocket(answers))


def test_position_without_purchase_price(tmp_path: Path) -> None:
    answers = default_answers()
    answers["compactPortfolioByType"] = lambda _: {
        "categories": [{"positions": [{"isin": "NL0010273215", "netSize": "10"}]}]
    }
    portfolio = fetch(tmp_path, TradeRepublicClient(PHONE, tmp_path / "s.cookies", "en"), FakeWebSocket(answers))
    assert portfolio.positions["NL0010273215"].gain is None
    assert portfolio.gain == 0
    assert portfolio.gain_percent is None


@pytest.mark.parametrize(
    "call",
    [
        lambda api: api.market_order("NL0010273215", "LSX", "buy", 1, "gfd", False),
        lambda api: api.limit_order("NL0010273215", "LSX", "sell", 1, 100, "gfd"),
        lambda api: api.stop_market_order("NL0010273215", "LSX", "sell", 1, 90, "gfd"),
        lambda api: api.cancel_order("order"),
        lambda api: api.create_savings_plan("NL0010273215", 50, "monthly", "2026-12-01", "dayOfMonth", 1),
        lambda api: api.change_savings_plan("plan", "NL0010273215", 50, "monthly", "2026-12-01", "dayOfMonth", 1),
        lambda api: api.cancel_savings_plan("plan"),
        lambda api: api.create_price_alarm("NL0010273215", 100),
        lambda api: api.add_watchlist("NL0010273215"),
        lambda api: api.timeline(),
    ],
)
def test_refuses_requests_that_are_not_allowed(tmp_path: Path, call: Callable[[ReadOnlyApi], Any]) -> None:
    ws = FakeWebSocket({})
    api = make_api(tmp_path, ws)
    with pytest.raises(ReadOnlyError):
        asyncio.run(call(api))
    assert ws.subscriptions == []


def test_refuses_payouts(tmp_path: Path) -> None:
    api = make_api(tmp_path, FakeWebSocket({}))
    with pytest.raises(ReadOnlyError):
        api.payout(100)
    with pytest.raises(ReadOnlyError):
        api.confirm_payout("process", "1234")


def test_no_login_without_pin(tmp_path: Path) -> None:
    api = ReadOnlyApi(PHONE, tmp_path / "session.cookies", "en")
    with pytest.raises(ReadOnlyError):
        api.initiate_weblogin()


class FakeResponse:
    """What pytr's _web_request returns: status and JSON."""

    def __init__(self, status: int, payload: Any = None) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def _ms(*args: int) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp() * 1000)


def _point(*args: int, value: str, gain: str | None = None) -> dict:
    point = {"timestamp": _ms(*args), "netValue": value}
    if gain is not None:
        point["performance"] = {"absoluteValue": gain, "relativeValue": "0"}
    return point


def _fetch_history(tmp_path: Path, web_request) -> list[HistoryPoint]:
    api = make_api(tmp_path, FakeWebSocket({}))
    api._web_request = web_request
    with patch.object(TradeRepublicClient, "_resume", return_value=api):
        return TradeRepublicClient(PHONE, tmp_path / "s.cookies", "en").fetch_history("EUR")


def test_reads_history(tmp_path: Path) -> None:
    def web_request(path: str) -> FakeResponse:
        url = urlparse(path)
        query = parse_qs(url.query)
        assert url.path == "/api-gateway/portfolio-chart/v2/chart"
        assert query["secAccNo"] == ["1234567890"]
        assert query["currency"] == ["EUR"]
        return {
            "max": FakeResponse(
                200,
                {
                    "points": [
                        # The curve is zero until the first purchase.
                        _point(2024, 5, 1, 0, 0, value="0.00", gain="0"),
                        _point(2025, 1, 1, 9, 15, value="100", gain="0"),
                        _point(2026, 10, 1, 9, 0, value="150", gain="40"),
                    ],
                    "openingTime": 0,
                },
            ),
            # Its performance counts from a year ago: it replaces the value of the same hour
            # but not the gain since the first purchase. Broken points are skipped.
            "1y": FakeResponse(
                200,
                {
                    "points": [
                        _point(2026, 10, 1, 9, 30, value="151", gain="5"),
                        _point(2026, 10, 2, 9, 30, value="152", gain="6"),
                        {"timestamp": "x"},
                    ]
                },
            ),
        }[query["range"][0]]

    assert _fetch_history(tmp_path, web_request) == [
        HistoryPoint(datetime(2025, 1, 1, 9, 15, tzinfo=UTC), 100.0, 0.0),
        HistoryPoint(datetime(2026, 10, 1, 9, 30, tzinfo=UTC), 151.0, 40.0),
        HistoryPoint(datetime(2026, 10, 2, 9, 30, tzinfo=UTC), 152.0, None),
    ]


def test_no_history(tmp_path: Path) -> None:
    with pytest.raises(TradeRepublicConnectionError, match="max: HTTP 404; 1y: HTTP 400"):
        _fetch_history(tmp_path, lambda path: FakeResponse(404 if "range=max" in path else 400))


def test_history_with_expired_session(tmp_path: Path) -> None:
    with pytest.raises(TradeRepublicAuthError):
        _fetch_history(tmp_path, lambda path: FakeResponse(401))
