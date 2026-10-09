"""Exercise the API client against a real E.ON Next account, outside Home Assistant.

    uv run --no-project --with aiohttp python scripts/prove_api.py [--env-file PATH]

Reads EON_EMAIL / EON_PASSWORD from the environment or a KEY=VALUE env file.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
LONDON = ZoneInfo("Europe/London")


def load_api():
    # Load by path: importing the package would pull in Home Assistant via __init__.py.
    spec = importlib.util.spec_from_file_location("eon_next_api", ROOT / "custom_components" / "eon_next_hh" / "api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{': ' + detail if detail else ''}")
    return ok


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=ROOT.parent / "eon-usage" / ".env")
    args = parser.parse_args()
    load_env_file(args.env_file)
    email, password = os.environ.get("EON_EMAIL"), os.environ.get("EON_PASSWORD")
    if not email or not password:
        print("EON_EMAIL and EON_PASSWORD must be set")
        return 2

    api = load_api()
    results: list[bool] = []
    async with aiohttp.ClientSession() as session:
        print("Login with email/password")
        client = api.EonNextClient(session)
        refresh = await client.login(email, password)
        results.append(check("refresh token issued", bool(refresh), f"expires {client.refresh_expires}"))

        print("Bad password is reported as an auth error")
        try:
            await api.EonNextClient(session).login(email, password + "x")
            results.append(check("rejected", False))
        except api.EonNextAuthError as err:
            results.append(check("rejected", True, str(err)))

        print("Accounts and meter points (fresh client, refresh token only)")
        rotated: list[str] = []
        client = api.EonNextClient(session, refresh, on_refresh_token=rotated.append)
        accounts = await client.account_numbers()
        results.append(check("accounts", bool(accounts), ", ".join(accounts)))
        print(f"  refresh token rotated on use: {bool(rotated)}; expires {client.refresh_expires}")
        meter_points = [mp for account in accounts for mp in await client.meter_points(account)]
        for mp in meter_points:
            print(f"  account {mp.account_number} property {mp.property_id} MPAN {mp.mpan} serials {', '.join(mp.serial_numbers)}")
        results.append(check("at least one import meter point", bool(meter_points)))
        mp = meter_points[0]

        print("Recent half-hourly readings")
        now = datetime.now(UTC)
        recent = await client.half_hourly_readings(mp, now - timedelta(days=7), now)
        if recent:
            print(f"  {len(recent)} readings, {recent[0].start.isoformat()} .. {recent[-1].start.isoformat()}, {sum(r.kwh for r in recent):.3f} kWh")
        results.append(check("readings returned", bool(recent)))
        results.append(check("all on half-hour boundaries", all(r.start.minute in (0, 30) and r.start.second == 0 for r in recent)))

        print("Paging (page size 50)")
        small = api.EonNextClient(session, client.refresh_token, page_size=50)
        paged = await small.half_hourly_readings(mp, now - timedelta(days=7), now)
        results.append(check("paged result matches single page", paged == recent, f"{len(paged)} readings"))

        print("Expired/invalid access token triggers a refresh")
        client._access_token = "not-a-valid-jwt"
        again = await client.account_numbers()
        results.append(check("recovered", again == accounts))

        print("Rejected refresh token falls back to the stored credentials")
        fallback = api.EonNextClient(session, "not-a-refresh-token", email=email, password=password, on_refresh_token=rotated.append)
        results.append(check("recovered", await fallback.account_numbers() == accounts and fallback.refresh_token != "not-a-refresh-token"))

        print("BST transitions (Europe/London days; skipped if no data that far back)")
        # E.ON reports the repeated 01:00 of a 25-hour day as one doubled reading, so 49 is expected there.
        for day, expected in [(datetime(2026, 3, 29), 46), (datetime(2025, 10, 26), 49), (datetime(2025, 3, 30), 46)]:
            start = day.replace(tzinfo=LONDON)
            end = (day + timedelta(days=1)).replace(tzinfo=LONDON)
            readings = await client.half_hourly_readings(mp, start, end)
            if not readings:
                print(f"  {day.date()}: no data")
                continue
            hours = Counter(r.start.astimezone(LONDON).strftime("%H") for r in readings)
            results.append(check(f"{day.date()} has {expected} half-hours", len(readings) == expected, f"got {len(readings)}; local 01:xx slots: {hours.get('01', 0)}"))

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
