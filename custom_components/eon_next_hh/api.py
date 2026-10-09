"""E.ON Next (Kraken) GraphQL client.

Deliberately free of Home Assistant imports so it can be reused outside HA.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

ENDPOINT = "https://api.eonnext-kraken.energy/v1/graphql/"
PAGE_SIZE = 1000  # larger pages are rejected with KT-CT-1202
MAX_RETRIES = 4
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)
EXPIRY_MARGIN = timedelta(seconds=60)
SLOT = timedelta(minutes=30)

AUTH_ERROR_CODES = {"KT-CT-1111", "KT-CT-1124", "KT-CT-1139", "KT-CT-1143", "KT-CT-4177", "UNAUTHENTICATED"}
RATE_LIMIT_CODES = {"KT-CT-1199"}

OBTAIN_TOKEN = """
mutation obtainKrakenToken($input: ObtainJSONWebTokenInput!) {
  obtainKrakenToken(input: $input) {
    token
    payload
    refreshToken
    refreshExpiresIn
  }
}
"""

ACCOUNTS = """
query accounts {
  viewer {
    accounts {
      number
    }
  }
}
"""

ELECTRICITY_METER_POINTS = """
query electricityMeterPoints($accountNumber: String!) {
  properties(accountNumber: $accountNumber) {
    id
    electricityMeterPoints {
      mpan
      direction
      meters(includeInactive: false) {
        serialNumber
      }
    }
  }
}
"""

HALF_HOURLY_MEASUREMENTS = """
query halfHourlyMeasurements(
  $propertyId: ID!
  $mpan: String!
  $startAt: DateTime!
  $endAt: DateTime!
  $first: Int!
  $after: String
) {
  property(id: $propertyId) {
    measurements(
      startAt: $startAt
      endAt: $endAt
      first: $first
      after: $after
      timezone: "Europe/London"
      utilityFilters: [
        {
          electricityFilters: {
            readingFrequencyType: THIRTY_MIN_INTERVAL
            readingDirection: CONSUMPTION
            marketSupplyPointId: $mpan
          }
        }
      ]
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      edges {
        node {
          value
          unit
          ... on IntervalMeasurementType {
            startAt
            durationInSeconds
          }
        }
      }
    }
  }
}
"""


class EonNextError(Exception):
    """An error reported by, or talking to, the E.ON Next API."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(f"{message} ({code})" if code else message)
        self.message = message
        self.code = code


class EonNextAuthError(EonNextError):
    """Credentials or refresh token were rejected; the user must sign in again."""


class EonNextConnectionError(EonNextError):
    """The API could not be reached, or kept failing transiently."""


class _Unauthorized(Exception):
    pass


class _Retryable(Exception):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class MeterPoint:
    account_number: str
    property_id: str
    mpan: str
    serial_numbers: tuple[str, ...]


@dataclass(frozen=True)
class Reading:
    start: datetime  # UTC
    kwh: float


class EonNextClient:
    """Authenticates with a refresh token and queries consumption.

    Refresh tokens expire a week after a password login and are not renewed by use, so when credentials are
    supplied the client signs in again once the refresh token is rejected. `on_refresh_token` is called
    whenever a new refresh token is issued so it can be persisted.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        refresh_token: str | None = None,
        *,
        email: str | None = None,
        password: str | None = None,
        on_refresh_token: Callable[[str], None] | None = None,
        endpoint: str = ENDPOINT,
        page_size: int = PAGE_SIZE,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session = session
        self._refresh_token = refresh_token
        self._credentials = (email, password) if email and password else None
        self._on_refresh_token = on_refresh_token
        self._endpoint = endpoint
        self._page_size = page_size
        self._sleep = sleep
        self._now = now
        self._access_token: str | None = None
        self._access_expires = datetime.min.replace(tzinfo=UTC)
        self.refresh_expires: datetime | None = None

    @property
    def refresh_token(self) -> str | None:
        return self._refresh_token

    async def login(self, email: str, password: str) -> str:
        """Exchange credentials for tokens and return the refresh token. These credentials are not retained."""
        await self._obtain_token({"email": email, "password": password})
        if not self._refresh_token:
            raise EonNextAuthError("Login succeeded but no refresh token was issued")
        return self._refresh_token

    async def account_numbers(self) -> list[str]:
        data = await self._query(ACCOUNTS)
        return [account["number"] for account in data["viewer"]["accounts"]]

    async def meter_points(self, account_number: str) -> list[MeterPoint]:
        """Import meter points only: export (generation) MPANs have no consumption."""
        data = await self._query(ELECTRICITY_METER_POINTS, {"accountNumber": account_number})
        return [
            MeterPoint(
                account_number=account_number,
                property_id=prop["id"],
                mpan=mp["mpan"],
                serial_numbers=tuple(m["serialNumber"] for m in mp["meters"]),
            )
            for prop in data["properties"]
            for mp in prop["electricityMeterPoints"]
            if mp.get("direction") != "EXPORT"
        ]

    async def all_meter_points(self) -> list[MeterPoint]:
        return [mp for account in await self.account_numbers() for mp in await self.meter_points(account)]

    async def half_hourly_readings(self, meter_point: MeterPoint, start: datetime, end: datetime) -> list[Reading]:
        """All half-hourly readings starting in [start, end), sorted, paging through the results."""
        readings: dict[datetime, Reading] = {}
        after: str | None = None
        while True:
            data = await self._query(
                HALF_HOURLY_MEASUREMENTS,
                {
                    "propertyId": meter_point.property_id,
                    "mpan": meter_point.mpan,
                    "startAt": start.astimezone(UTC).isoformat(),
                    "endAt": end.astimezone(UTC).isoformat(),
                    "first": self._page_size,
                    "after": after,
                },
            )
            if data["property"] is None:
                raise EonNextError(f"Property {meter_point.property_id} not found")
            measurements = data["property"]["measurements"]
            for edge in measurements["edges"]:
                reading = _to_reading(edge["node"])
                if start <= reading.start < end:
                    readings[reading.start] = reading
            page_info = measurements["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            after = page_info["endCursor"]
        return [readings[k] for k in sorted(readings)]

    async def _query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        reauthenticated = False
        while True:
            token = await self._ensure_access_token()
            try:
                return await self._with_retries(lambda: self._post(query, variables, token))
            except _Unauthorized as err:
                if reauthenticated:
                    raise EonNextAuthError(str(err)) from None
                reauthenticated = True
                self._access_token = None

    async def _ensure_access_token(self) -> str:
        if self._access_token and self._access_expires - EXPIRY_MARGIN > self._now():
            return self._access_token
        if self._refresh_token:
            try:
                return await self._obtain_token({"refreshToken": self._refresh_token})
            except EonNextAuthError:
                if not self._credentials:
                    raise
                _LOGGER.debug("Refresh token rejected; signing in again")
        if not self._credentials:
            raise EonNextAuthError("Not signed in")
        email, password = self._credentials
        return await self._obtain_token({"email": email, "password": password})

    async def _obtain_token(self, token_input: dict[str, str]) -> str:
        try:
            data = await self._with_retries(lambda: self._post(OBTAIN_TOKEN, {"input": token_input}, None))
        except (_Unauthorized, EonNextAuthError) as err:
            raise EonNextAuthError(str(err)) from None
        except EonNextConnectionError:
            raise
        except EonNextError as err:
            # Any non-transient failure to obtain a token means the credentials or refresh token are no good.
            raise EonNextAuthError(err.message, err.code) from None

        result = data["obtainKrakenToken"]
        payload = result["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        self._access_token = result["token"]
        self._access_expires = datetime.fromtimestamp(payload["exp"], UTC)
        if result.get("refreshExpiresIn"):
            self.refresh_expires = datetime.fromtimestamp(result["refreshExpiresIn"], UTC)
        new_refresh = result.get("refreshToken")
        if new_refresh and new_refresh != self._refresh_token:
            self._refresh_token = new_refresh
            if self._on_refresh_token:
                self._on_refresh_token(new_refresh)
        return result["token"]

    async def _with_retries(self, request: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        for attempt in range(MAX_RETRIES + 1):
            try:
                return await request()
            except _Retryable as err:
                if attempt == MAX_RETRIES:
                    raise EonNextConnectionError(str(err)) from None
                delay = err.retry_after or 2**attempt
                _LOGGER.debug("E.ON Next request failed (%s); retrying in %ss", err, delay)
                await self._sleep(delay)
        raise AssertionError("unreachable")

    async def _post(self, query: str, variables: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
        headers = {"Authorization": f"JWT {token}"} if token else {}
        try:
            async with self._session.post(
                self._endpoint,
                json={"query": query, "variables": variables or {}},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                if resp.status == 401:
                    raise _Unauthorized("HTTP 401")
                if resp.status == 429 or resp.status >= 500:
                    raise _Retryable(f"HTTP {resp.status}", retry_after)
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    if resp.status == 403:
                        raise _Retryable("HTTP 403", retry_after) from None
                    raise EonNextError(f"HTTP {resp.status}: response was not JSON") from None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise _Retryable(f"Network error: {type(err).__name__}") from None

        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            first = errors[0]
            extensions = first.get("extensions") or {}
            code = extensions.get("errorCode") or extensions.get("code")
            message = first.get("message") or f"HTTP {resp.status}"
            if code in RATE_LIMIT_CODES:
                raise _Retryable(message, retry_after)
            if code in AUTH_ERROR_CODES or "signature has expired" in message.lower():
                raise _Unauthorized(message)
            raise EonNextError(message, code)
        if resp.status >= 400 or not isinstance(body, dict) or "data" not in body:
            raise EonNextError(f"HTTP {resp.status}: unexpected response")
        return body["data"]


def _parse_retry_after(value: str | None) -> float | None:
    try:
        seconds = float(value) if value else 0
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def _to_reading(node: dict[str, Any]) -> Reading:
    if not node.get("startAt") or node.get("durationInSeconds") != SLOT.total_seconds() or str(node.get("unit")).lower() != "kwh":
        raise EonNextError(f"Unexpected measurement: {node}")
    return Reading(start=datetime.fromisoformat(node["startAt"]).astimezone(UTC), kwh=float(node["value"]))
