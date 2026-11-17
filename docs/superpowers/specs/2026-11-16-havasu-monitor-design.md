# Havasu Campground Permit Monitor Design

Date: 2026-11-16
Status: approved

## Purpose and scope

The monitor sends a Discord alert when a Havasupai campground permit becomes
bookable for an arrival date Jacob is watching. Most of these openings are
cancellations. It only notifies. Booking stays manual.

- Campground permits only. Lodge rooms are out of scope.
- Production watch ranges are arrival dates **2027-05-08 to 2027-05-31** and
  **2027-10-01 to 2027-10-24**. A range bounds only the arrival date: an Oct 24
  arrival, departing Oct 27, is in range.
- For testing, the ranges are widened to every date, including the rest of 2026.
- Alerts go to a new channel's webhook with no `@everyone`.
- Polling runs every 10 seconds.

## Source

### Selected source

The reservation site, `havasupaireservations.com`, sends booking to NewBook at
`https://bookingsus.newbook.cloud/online/havasupai`. Availability is public. It
needs no account, only the session cookie that the booking page sets.

1. **Bootstrap.** `GET /online/havasupai` sets the session cookie. The page also
   contains `newbook_api_path`, a URL of the form
   `.../online/generated_api_files/<hash>.php?`. The hash is generated, so it is
   read from the page on every bootstrap and never hard-coded.
2. **Calendar.** `POST <api_path>newbook_api_action=data` with the booking-form
   fields (`available_from`, `available_to`, `nights=3`, `adults=1`) plus:
   - `query=newbook_calendar_update_table_dates`
   - `category_id=1` (Campground Permits)
   - `look_up_date=YYYY-MM`
   - `period_from=YYYY-MM` and `period_to=YYYY-MM`

   The response is JSON. `calendar_data.table_rows` holds HTML table rows with
   one `<td class="day …" data-date="YYYY-MM-DD">` per day, covering every month
   from `period_from` to `period_to`. One request covered nine months during
   discovery. `available_from` and `look_up_date` do not change the cells: a live
   request with both moved to May 2027 returned the same days. Both are sent as
   the first requested month.
3. **Per-arrival chart** (discovery only).
   `POST <api_path>newbook_api_action=availability_chart_responsive&force_category_id=1&…`
   returns either `"Book now"` with a price, or "There are currently no Sites
   available".

### Day classes

Observed on 2026-11-16:

| Class | Meaning |
|---|---|
| `day booked` | No permits that night: sold out, out of season, or not yet on sale. All three look identical. |
| `day available` | At least one permit is left that night. |
| `closed_departure` (modifier on `available`) | No departures that day. |
| `closed_arrival` (modifier) | No arrivals that day. Seen on 2026-12-01 (`day booked closed_arrival`), only when the query spans the Nov/Dec boundary. |

Every 2027 date currently shows `day booked`, because the 2027 season is not on
sale yet. Any class token the parser does not recognize is a fault, not a
guess.

### Arrival rule

A permit is always 3 nights. **Arrival D is open when nights D, D+1 and D+2 are
all `available`, D does not carry `closed_arrival`, and D+3 does not carry
`closed_departure`.** Departure-day
availability does not matter.

Checked against the per-arrival chart on 2026-11-16:

- Nov 16 and Nov 28 returned "Book now".
- Nov 29 and Nov 30 returned no sites, because Dec 1 is `booked`.

### Party size

The help center says a booking holds **one permit at a time**. A group clicks
"Add Booking" once per person before checking out. The chart enforces this:
`adults>1` gives "exceeded the Occupant Limit … (Maximum: 1)". The calendar
returned the same answer for 1, 4 and 12 people.

As a result:

- Availability is always queried with `adults=1`. Party size is not part of the
  query or the key.
- The calendar is binary. **No public endpoint shows how many permits are left**,
  so an alert cannot say whether one person can go or six.
- The cart endpoints might reveal a count, but adding to a cart can hold
  inventory away from real bookers. The monitor never calls any `cart` action.

### Request budget

Each tick makes one calendar POST, with `period_from` and `period_to` set to the
first and last months the watch ranges touch (`2027-05` to `2027-10` in
production). At 10s with the runner's 20% jitter, that is about 8,600 requests a
day. The bootstrap GET runs only at startup and after a session-level failure.

## Application design

`apps/havasu` uses the four-method `Monitor` protocol. Melanzana, the other
10-second calendar monitor, is the model to copy.

- `types.py`: `OpenArrival(date: datetime.date)`.
- `newbook.py`: bootstrap (cookie and API path), the calendar POST, and the
  strict `<td>` parser that returns `dict[date, DayState]`.
- `detector.py`: the arrival rule and the watch-range cut. These are pure
  functions.
- `monitor.py`:
  - `fetch()`: bootstrap if needed, one calendar call, then derive the open
    arrivals.
  - `key()`: `campground:YYYY-MM-DD`.
  - `render()`: one message for all fresh arrivals.
- `alert.py`: one green embed with one inline field per arrival date, using
  melanzana's 24-field cap and overflow note. The title links to the booking
  page. The footer says to add one permit per person.
- `config.py`: holds the app settings below. The runner supplies the poll and
  webhook settings.

**Reopens alert again.** The runner's baseline is the current key set, so a date
that sells out and later reopens comes back as fresh.

**Sale-day flood.** On Feb 1 and at the early-access release, every watched
date flips from `booked` to `available` at once. That arrives as one message
with the overflow note.

### Faults

Each of these raises, so the runner backs off and leaves the baseline alone:

- The API path is missing from the bootstrap page.
- The response is not JSON, or has no `table_rows`.
- A non-200 response, or an echoed period that differs from the request.
- Any day of a requested month is missing or repeated.
- A cell has an unknown class token or an unparseable date.

Any fault also clears the session, so the next tick bootstraps again. That one
rule covers an expired cookie, a rotated API path and an outage without guessing
which one it was.

### Configuration

| Variable | Default | Notes |
|---|---|---|
| `WATCH_RANGES` | `2027-05-08..2027-05-31,2027-10-01..2027-10-24` | Inclusive arrival ranges. Exactly `all` means every date the calendar returns, for testing. The production ranges live only in this app default; Terraform passes the variable only when it is set. |
| `POLL_INTERVAL_SEC` | `10` | Runner setting. |
| `BOOKING_URL` | `https://bookingsus.newbook.cloud/online/havasupai` | Link in the alert title. |
| `DISCORD_WEBHOOK_URL` | — | New channel. Secret. |

`all` needs explicit bounds for the calendar query. It covers the current month
through December of next year.

## Testing

The fixtures are captured calendar responses from 2026-11-16:

- March–November 2026: 260 booked days, then Nov 16–30 available.
- December 2026–December 2027: every day booked.

Tests cover:

- The parser, including unknown-class and missing-month faults.
- The arrival rule: Nov 28 open, Nov 29 closed, plus a `closed_departure` on
  D+3.
- Range cuts.
- Render overflow.
- The four methods.

Live testing runs `WATCH_RANGES=all` locally. Nov 16–28 2026 is open right now,
so `echo '[]' > state.json` triggers a real alert.

## Deployment

The steps follow `apps/README.md`:

1. Workspace member and app package.
2. `infra/variables.tf` webhook variable and `local.app_env.havasu`.
3. `apps.auto.tfvars` entry.
4. Create the registry repository.
5. Push the amd64 image.
6. Run `./infra/deploy.sh`.

Production steps run only with Jacob's go-ahead.

## Out of scope

- Auto-booking and any cart interaction.
- Lodge permits.
- Early-access lottery reminders. Jacob handles those himself.
- Remaining-permit counts, which no public endpoint provides.
