# E.ON Next Half-Hourly

A Home Assistant custom integration that imports your E.ON Next smart meter's half-hourly electricity consumption into Home Assistant's long-term statistics. It splits the data into peak and off-peak usage, so the Energy dashboard can price each at its own rate.

On first setup it backfills your history, then it polls every 4 hours to stay current.

## What you get

For each meter, three external statistics (kWh, hourly, with running totals):

| Statistic ID                   | Contents                                        |
| ------------------------------ | ----------------------------------------------- |
| `eon_next_hh:<mpan>_total`     | All consumption                                 |
| `eon_next_hh:<mpan>_peak`      | Half-hours outside every off-peak window        |
| `eon_next_hh:<mpan>_offpeak`   | Half-hours inside an off-peak window            |

`peak + offpeak = total` exactly.

There are also three diagnostic sensors on the meter's device:

- **Last data timestamp**: the end of the most recent half-hour E.ON Next has published. Expect this to trail real time by a day or two.
- **Last sync status**: `ok`, `backfilling`, `error`, `reauth_required` or `pending`. Its attributes show the error detail, the last successful sync and the backfill progress (%).
- **Missing intervals (7 days)**: how many half-hours are absent from the last 7 days, up to the latest reading. The `missing` attribute lists the gaps as ISO 8601 `start/end` ranges.

## Installation

Requires Home Assistant 2026.9 or later.

### HACS

1. In HACS, open the menu → **Custom repositories**, add this repository's URL and choose type **Integration**.
2. Install **E.ON Next Half-Hourly**, then restart Home Assistant.

HACS only works with public GitHub repositories. If you keep this repository private, use the manual install instead.

### Manual

Copy `custom_components/eon_next_hh` into your Home Assistant `config/custom_components/` folder and restart.

## Setup

**Settings → Devices & services → Add integration → E.ON Next Half-Hourly**, then enter:

- **Email** and **password** for your E.ON Next account.
- **Days of history to import** (default 180).

If your account has more than one electricity import meter, you'll be asked which one to use. Add the integration again for each extra meter. Export (generation) meters are ignored.

The first sync runs in the background. It fetches history in 7-day chunks, newest first, with a short pause between requests, and saves its progress after every chunk. If Home Assistant restarts part-way through, it carries on from where it stopped. The statistics appear once the backfill completes.

### Why the password is stored

E.ON Next's refresh tokens expire a week after you sign in with your password, and using a refresh token does not extend it. The integration uses the refresh token while it's valid. When the token is rejected it signs in again with the stored password, so it keeps working without asking you every week. The password and tokens are kept in the config entry like any other Home Assistant cloud integration's credentials, and are never logged. If the password stops working, for example because you changed it, Home Assistant shows a re-authentication prompt.

## Off-peak windows

**Settings → Devices & services → E.ON Next Half-Hourly → Configure.**

Enter one local (UK) time range per line, such as:

```
00:00-07:00
13:00-16:00
```

- Ranges may cross midnight, e.g. `23:30-05:30`.
- Times must be on the hour or half-hour. `24:00` means midnight.
- With no windows, everything counts as peak.

Each half-hour is classified by the UK wall-clock time at which it starts. On the day the clocks go forward, 01:00–02:00 doesn't exist, so a `00:00-07:00` window covers 6 hours. On the day they go back, 01:00–02:00 happens twice and both passes count, so it covers 8 hours. Classified half-hours are then added into hourly UTC buckets. UK offsets are whole hours, so these line up with local hours.

Changing the windows reclassifies the last 3 days straight away. To reclassify older history, run the backfill action below over the dates you care about.

## Energy dashboard: peak and off-peak at different prices

1. Go to **Settings → Dashboards → Energy**. Under **Electricity grid**, choose **Add consumption**.
2. Pick **E.ON Next `<mpan>` peak**. Under cost, choose **Use a static price** and enter your peak unit rate in £/kWh, then save.
3. Choose **Add consumption** again, pick **E.ON Next `<mpan>` off-peak** and enter your off-peak unit rate.

Don't also add the **total** statistic as a source, or your consumption will be counted twice. Standing charges aren't included, because the Energy dashboard prices energy only.

If you'd rather see a single grid line with no tariff split, add only the total statistic instead.

## Keeping current and fixing late data

Every 4 hours the integration:

1. Finds the last stored hour before a re-fetch window (now minus 3 days), and the running totals at that hour.
2. Fetches everything after that point, plus the last 7 days for the missing-interval sensor.
3. Re-imports every hour after that point, continuing the totals.

Readings that arrive late, or are corrected, within 3 days therefore fix themselves. Totals are calculated in whole micro-kWh, so they come out identical whatever order the data arrives in.

For gaps E.ON fills in after more than 3 days (watch the missing-intervals sensor), re-import the affected dates:

```yaml
action: eon_next_hh.backfill
data:
  start_date: "2026-03-01"
  end_date: "2026-03-07"
  # config_entry_id: ...   # optional: one meter only; defaults to all
```

Dates are UK local and inclusive. After re-importing the range, the action recalculates the running totals of every later hour from their stored hourly values, so later history stays consistent without re-fetching it.

### A quirk of E.ON's data on the day the clocks go back

E.ON Next reports the repeated 01:00 local half-hour as a single doubled reading on the first pass (01:00 BST) and returns nothing for 01:00 GMT. The day's total is correct, and both passes are off-peak or peak together, so the split is unaffected. Only that hour's consumption sits one UTC hour early. The missing-intervals sensor doesn't count it as a gap.

## Development

```
custom_components/eon_next_hh/
  api.py           Kraken GraphQL client (aiohttp, no Home Assistant imports)
  buckets.py       window parsing, classification, bucketing, running totals (no HA imports)
  stats.py         recorder reads and writes
  coordinator.py   backfill, rolling re-import, range re-import, diagnostics
  config_flow.py   config, meter choice, reauth and options flows
  sensor.py        diagnostic sensors
scripts/
  prove_api.py     exercises the client against a real account
  wsl-test.sh      runs the test suite under WSL
tests/             pytest-homeassistant-custom-component tests, fixture data only
```

`api.py` and `buckets.py` don't import Home Assistant, so you can reuse them from a CLI. `scripts/prove_api.py` shows how to load `api.py` directly:

```
uv run --no-project --with aiohttp --with tzdata python scripts/prove_api.py 
```

That reads `EON_EMAIL` and `EON_PASSWORD`, from the environment or a `.env` file in the repository root (`--env-file` to use another), then checks login, token refresh, recovery from a rejected token, paging and the BST days against your real account. E.ON rate-limits logins ("Too many requests"), so don't run it repeatedly in quick succession.

Tests make no network calls. Home Assistant doesn't run on native Windows, so on Windows run them under WSL:

```
wsl -d Ubuntu-22.04 --exec bash -lc 'bash /mnt/d/path/to/eon_next_hh/scripts/wsl-test.sh -q'
```

On Linux or macOS: `uv sync && uv run pytest`.

Authentication (`obtainKrakenToken` with email/password, then `refreshToken`) matches [PhilTee/homeassistant_eon_next](https://github.com/PhilTee/homeassistant_eon_next), [madmachinations/eon-next](https://github.com/madmachinations/eon-next) and the `eonapi` package.

`eonapi` reads half-hourly data from `meters { consumption(grouping: HALF_HOUR) }`. This integration uses `property.measurements` filtered to the meter's MPAN, `THIRTY_MIN_INTERVAL` and `CONSUMPTION`, because it takes an explicit end time and never mixes in export data. Neither field is deprecated, according to the live API's introspection.
