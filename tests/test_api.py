"""The Kraken client, against a scripted fake server (no Home Assistant, no network)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from custom_components.eon_next_hh.api import (
    EonNextAuthError,
    EonNextClient,
    EonNextConnectionError,
    MeterPoint,
    Reading,
)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
METER = MeterPoint("A-1234ABCD", "1234567", "1900000000001", ("21E0000001",))


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def token_response(token: str, refresh: str | None = "refresh-1", exp: datetime = NOW.replace(hour=13)) -> dict[str, Any]:
    return {
        "data": {
            "obtainKrakenToken": {
                "token": token,
                "payload": {"sub": "kraken|account-user:1", "exp": int(exp.timestamp())},
                "refreshToken": refresh,
                "refreshExpiresIn": int(NOW.timestamp()) + 7 * 86400,
            }
        }
    }


class FakeResponse:
    def __init__(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def json(self, content_type: str | None = None) -> Any:
        if isinstance(self._body, str):
            raise ValueError("not JSON")
        return self._body


class FakeServer:
    """Answers each POST by calling `handler(operation, variables, auth_header)`."""

    def __init__(self, handler: Callable[[str, dict[str, Any], str | None], FakeResponse | dict[str, Any]]) -> None:
        self.handler = handler
        self.requests: list[tuple[str, dict[str, Any], str | None]] = []

    def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: Any) -> FakeResponse:
        operation = re.search(r"(?:query|mutation)\s+(\w+)", json["query"]).group(1)
        auth = headers.get("Authorization")
        self.requests.append((operation, json["variables"], auth))
        result = self.handler(operation, json["variables"], auth)
        return result if isinstance(result, FakeResponse) else FakeResponse(200, result)

    def operations(self) -> list[str]:
        return [op for op, _, _ in self.requests]


async def no_sleep(seconds: float) -> None:
    return None


def make_client(server: FakeServer, **kwargs: Any) -> EonNextClient:
    return EonNextClient(server, sleep=no_sleep, now=lambda: NOW, **kwargs)  # type: ignore[arg-type]


async def test_login_returns_refresh_token_without_keeping_credentials() -> None:
    server = FakeServer(lambda op, v, auth: token_response("access-1"))
    client = make_client(server)
    assert await client.login("me@example.com", "hunter2") == "refresh-1"
    assert server.requests == [("obtainKrakenToken", {"input": {"email": "me@example.com", "password": "hunter2"}}, None)]
    assert "hunter2" not in repr(vars(client))


async def test_invalid_credentials() -> None:
    server = FakeServer(lambda op, v, auth: fixture("error_invalid_credentials.json"))
    with pytest.raises(EonNextAuthError, match="KT-CT-1138"):
        await make_client(server).login("me@example.com", "wrong")


async def test_accounts_and_import_meter_points() -> None:
    def handler(op: str, v: dict[str, Any], auth: str | None) -> dict[str, Any]:
        if op == "obtainKrakenToken":
            return token_response("access-1")
        assert auth == "JWT access-1"
        return fixture("accounts.json") if op == "accounts" else fixture("meter_points.json")

    client = make_client(FakeServer(handler), refresh_token="refresh-1")
    assert await client.all_meter_points() == [METER]


async def test_pages_through_measurements() -> None:
    def handler(op: str, v: dict[str, Any], auth: str | None) -> dict[str, Any]:
        if op == "obtainKrakenToken":
            return token_response("access-1")
        return fixture("measurements_page2.json") if v["after"] else fixture("measurements_page1.json")

    server = FakeServer(handler)
    client = make_client(server, refresh_token="refresh-1")
    readings = await client.half_hourly_readings(
        METER, datetime(2025, 10, 25, 23, tzinfo=UTC), datetime(2025, 10, 26, 3, tzinfo=UTC)
    )
    assert readings == [
        Reading(datetime(2025, 10, 25, 23, 0, tzinfo=UTC), 0.352),
        Reading(datetime(2025, 10, 25, 23, 30, tzinfo=UTC), 0.436),
        Reading(datetime(2025, 10, 26, 0, 0, tzinfo=UTC), 0.842),
        Reading(datetime(2025, 10, 26, 0, 30, tzinfo=UTC), 0.405),
        Reading(datetime(2025, 10, 26, 1, 30, tzinfo=UTC), 0.398),
        Reading(datetime(2025, 10, 26, 2, 0, tzinfo=UTC), 0.377),
    ]
    measurement_calls = [v for op, v, _ in server.requests if op == "halfHourlyMeasurements"]
    assert [v["after"] for v in measurement_calls] == [None, "YXJyYXljb25uZWN0aW9uOjI="]
    assert measurement_calls[0]["startAt"] == "2025-10-25T23:00:00+00:00"


@pytest.mark.parametrize("failure", ["http_401", "expired_jwt"])
async def test_unauthorised_request_refreshes_token_and_retries(failure: str) -> None:
    issued = iter(["access-1", "access-2"])

    def handler(op: str, v: dict[str, Any], auth: str | None) -> FakeResponse | dict[str, Any]:
        if op == "obtainKrakenToken":
            assert v["input"] == {"refreshToken": "refresh-1"}
            return token_response(next(issued))
        if auth == "JWT access-1":
            return FakeResponse(401, "Unauthorized") if failure == "http_401" else fixture("error_expired_token.json")
        return fixture("accounts.json")

    server = FakeServer(handler)
    assert await make_client(server, refresh_token="refresh-1").account_numbers() == ["A-1234ABCD"]
    assert server.operations() == ["obtainKrakenToken", "accounts", "obtainKrakenToken", "accounts"]


async def test_rejected_refresh_token_falls_back_to_password_and_reports_new_token() -> None:
    saved: list[str] = []

    def handler(op: str, v: dict[str, Any], auth: str | None) -> dict[str, Any]:
        if op == "obtainKrakenToken":
            if "refreshToken" in v["input"]:
                return fixture("error_invalid_credentials.json")
            return token_response("access-2", refresh="refresh-2")
        return fixture("accounts.json")

    client = make_client(
        FakeServer(handler), refresh_token="expired", email="me@example.com", password="pw", on_refresh_token=saved.append
    )
    assert await client.account_numbers() == ["A-1234ABCD"]
    assert saved == ["refresh-2"]


async def test_rejected_refresh_token_without_password_needs_reauth() -> None:
    server = FakeServer(lambda op, v, auth: fixture("error_invalid_credentials.json"))
    with pytest.raises(EonNextAuthError):
        await make_client(server, refresh_token="expired").account_numbers()


async def test_access_token_is_reused_until_near_expiry() -> None:
    server = FakeServer(lambda op, v, auth: token_response("access-1") if op == "obtainKrakenToken" else fixture("accounts.json"))
    client = make_client(server, refresh_token="refresh-1")
    await client.account_numbers()
    await client.account_numbers()
    assert server.operations() == ["obtainKrakenToken", "accounts", "accounts"]


async def test_rate_limit_and_server_errors_are_retried() -> None:
    failures = iter([fixture("error_rate_limited.json"), FakeResponse(503, "busy", {"Retry-After": "3"})])
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    def handler(op: str, v: dict[str, Any], auth: str | None) -> FakeResponse | dict[str, Any]:
        if op == "obtainKrakenToken":
            return token_response("access-1")
        return next(failures, fixture("accounts.json"))

    client = EonNextClient(FakeServer(handler), refresh_token="refresh-1", sleep=sleep, now=lambda: NOW)  # type: ignore[arg-type]
    assert await client.account_numbers() == ["A-1234ABCD"]
    assert delays == [1, 3]


async def test_persistent_failure_raises_connection_error() -> None:
    server = FakeServer(lambda op, v, auth: FakeResponse(502, "bad gateway"))
    with pytest.raises(EonNextConnectionError):
        await make_client(server, refresh_token="refresh-1").account_numbers()
