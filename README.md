# Public Service Info

A small Flask app that warns Novi Sad residents by email when power or water
outages are announced for their address.

Users sign in with Google, save their street address, and the app mails them
whenever an announced outage for the next day covers that address.

## How it works

1. Your external timer calls `/run_check` with the shared secret from
   `TRIGGER_TOKEN`, for example every hour. Calls before `CHECK_START_HOUR`
   (default 19:00, Europe/Belgrade) return HTTP 200 without checking or sending mail.
   Each permitted CGI request performs one synchronous pass; the app schedules no retries.
2. `checker.py` fetches the daily power outage page (`POWER_CHECK_URL`) and
   looks for the water works announcement matching tomorrow's date on
   `WATER_CHECK_URL`.
3. The app collects all unique, nonblank addresses belonging to approved users.
   It sends the full address list and announcement to Gemini once per available
   service (power/water), requesting JSON with a result for every address. The
   model handles Cyrillic/Latin matching and house-number ranges in bulk.
4. After validating the results, the app fetches the current approved users and
   matches their addresses to affected addresses. Each affected user gets one
   email combining the matching services and outage times. Users sharing an
   address reuse the same result; their emails are not sent to Gemini.
5. Each successful service check and its pending notifications are saved together
   in SQLite before sending email. When both service checks succeed, `checks`
   records the day's analysis as complete, even if SMTP fails. Subsequent URL
   calls only retry pending emails. If a service check failed, only that service
   is checked again; successful checks and deliveries are never repeated.
6. Dates use `Europe/Belgrade`. The power page's announcement heading must match
   tomorrow before it is processed. Gemini also checks the target date and must
   return correctly typed JSON. Water links must exactly match a parsed link from
   the configured water website; only `NEMA` means no announcement was found.

## Bulk Gemini response

Each address receives a request-local ID, for example:

```json
[
  {"address_id": "a1", "address": "Bulevar oslobođenja 12, Novi Sad"},
  {"address_id": "a2", "address": "Futoška 24, Novi Sad"}
]
```

For each service, Gemini returns:

```json
{
  "date_matches": true,
  "results": [
    {"address_id": "a1", "outage": true, "details": "08:00–10:00"},
    {"address_id": "a2", "outage": false, "details": ""}
  ]
}
```

IDs map back to the exact stored address; response order and rewritten street
names cannot change recipient matching. Identical address strings are deduplicated;
differently written addresses remain separate entries. Every submitted ID must
appear exactly once, including unaffected addresses. Missing, duplicate, unknown,
or incorrectly typed results reject the entire service response. An affected
address must have nonempty details, and the announcement date must be confirmed.
Valid results from the other service can still be delivered when one service fails.

With both pages available, a run normally makes **three Gemini requests**: one to
select the water announcement and two bulk address checks, regardless of user
count. No water announcement means there is no water address-check request.
The first request saves that day's address list. Later requests reuse it and only
check services that have not succeeded. A successful no-outage result (including
no water announcement) is saved too. New addresses are included on the next day;
they do not cause a successful daily check to repeat. Oversized, truncated, or
incomplete responses fail the service check rather than silently omitting addresses.

## Accounts

Google OAuth handles sign-in; there are no passwords. New users pick an
address, and every admin gets an email with a one-click approval link. Users
have three levels: `0` pending, `1` approved, `2` admin. `ADMIN_EMAIL` is
seeded as the first admin when the database is created. Admins can approve,
promote, remove users and edit their addresses at `/manage_users`.

Registration uses the verified Google email from a signed, 15-minute session;
email fields supplied by the browser cannot choose an account. Existing accounts
are never overwritten. Pending users cannot access account pages. Approval links
use a separate, single-use token; both approval methods clear that token and send
the same approval email. Approval does not log the user in: they sign in with
Google afterwards. If the approval email fails, the account remains approved and
the error is logged. Logout revokes the login token.

In debug mode (`application.debug`), OAuth is skipped: `/login` signs you in as
`local@localhost` with admin rights.

The UI is in Serbian.

## Files

| File | Purpose |
| --- | --- |
| `index.py` | Flask app, routes, OAuth, `/run_check` endpoint |
| `checker.py` | Fetches announcements, asks Gemini, sends notifications |
| `database.py` | SQLite accounts, daily checks, saved results, email outbox, and delivery history |
| `helper.py` | Token generation, HTTP fetch, SMTP sending, time formatting |
| `cgi_serve.py` | CGI entry point for shared hosting |
| `appsettings.py` | Configuration (not in git) |
| `client_secret.json` | Google OAuth client secrets (not in git) |

## Setup

```sh
pip install flask authlib flask-wtf httpx
cp appsettings.py.example appsettings.py   # then fill it in
```

Download the OAuth client secrets from the Google Cloud console and save them
as `client_secret.json`, with `https://<your-host>/oauth2callback` registered as
a redirect URI.

The database is created and migrated automatically on app startup or a checker
run. Back up the SQLite file before upgrading an existing installation. The token
migration preserves pending approval links and invalidates all old login tokens
once, since older admin approvals could leave approval tokens usable for login.
Existing users will need to sign in again. Accounts, addresses, and completed
check history are preserved.

## Running

```sh
python index.py                      # development, port 5000
gunicorn -w 2 index:application      # production
```

On CGI hosting, `cgi_serve.py` is the entry point. `safe_url_for()` in
`index.py` strips the CGI `SCRIPT_NAME` prefix that Flask's `url_for` would
otherwise double up.

Trigger a check with:

```sh
curl "https://<your-host>/run_check?token=<TRIGGER_TOKEN>"
```

It returns a plain text report, and HTTP 500 if any part of the run failed.


## CGI requests and evening retries

Your timer can call `/run_check?token=<TRIGGER_TOKEN>` every hour. Set the earliest
permitted hour in `appsettings.py`:

```python
CHECK_START_HOUR = 19
```

This must be an integer from 0 to 23. It defaults to 19 when omitted. The hour uses
`Europe/Belgrade`, including daylight-saving changes, regardless of the server's
timezone. Calls before 19:00 return HTTP 200 with a not-started message; they do not
fetch pages, call Gemini, attempt emails, or mark a daily check as completed.
At 19:00 and later, normal checks and pending-email retries are allowed. There is
no separate end hour. Once checks and email delivery succeed, further calls that
day do nothing. Setting the hour to 0 allows calls at any time.

No daemon, worker queue, sleeping request, or in-process retry timer is required.
The external timer controls the number and timing of attempts.

| State when the URL is called | Action in that request |
| --- | --- |
| Before `CHECK_START_HOUR` | Return HTTP 200 without checking or attempting email delivery. |
| No check started today, and start hour reached | Save the address list, check both services, save results and notifications, then attempt email delivery. |
| A service failed previously | Retry only the failed service; reuse successful results and attempt pending emails. |
| Checks succeeded but email failed/interrupted | Read the stored notifications and retry only unsent email. No website fetching or Gemini calls. |
| Checks and all emails succeeded | Return HTTP 200 without checking or sending again. |
| New calendar day | Start a new check for tomorrow's outages. |

Daily state uses `Europe/Belgrade`. It is stored in SQLite and survives CGI process
exit: `check_batches` holds the address list, `service_checks` holds successful
results, `checks` marks completed daily analysis, and `notification_outbox` holds
pending/sent/cancelled notifications and delivery attempts. The existing
`deliveries` history remains authoritative for older sent notifications.

The service result and pending notifications commit in one transaction. Completed
analysis is recorded before SMTP, so even a killed CGI request can resume sending
on the next URL call. Pending services for the same user/address are combined in
one email; a request attempts that email at most once. HTTP 500 means a check or
email attempt failed, while HTTP 200 means there was no failure in this request.
The response also reports whether analysis is complete and how many emails remain.

Pending emails are cancelled if the user was removed, is no longer approved, or
changed address. New addresses wait until the next day's check. Retries only send
notifications from today's check; older unsent notifications remain in the database
for inspection and are not sent as stale alerts on a later day. Nothing runs again until your timer (or you) calls the URL; each new day waits
until the configured start hour.

Run on Linux/Unix with a local SQLite database; all CGI processes must use the same
database path. A nonblocking `flock` on `<database path>.check.lock` prevents overlap
and is released by the OS on process exit or a crash. Keep that file in place while
the app is running. An overlapping trigger returns HTTP 500 for a later URL retry.
Ensure the CGI host permits enough request time for the API calls and email sends.

SMTP cannot guarantee exactly-once delivery: if the mail server accepts a message
but the connection fails, or the process stops before the delivery record commits,
a retry can still send that message again. This does not repeat the Gemini check.
Existing completed days and delivery records are preserved on upgrade; previously
failed emails without saved contents cannot be reconstructed from the old schema.

## Tests

```sh
python -m unittest discover -s tests -v
```

Tests use temporary SQLite databases, mock Google identity responses and SMTP,
and block real HTTP requests. They cover registration, approval, migration, CSRF,
date/model validation, bulk result matching, shared addresses, recipient changes,
retry deduplication, atomic outbox writes, four successive URL triggers,
a fresh CGI process retrying saved email, start-hour boundaries in summer/winter,
cross-process locking, and HTTP error
reporting. They do not require local secrets or send emails.

## Google Maps address preview (no API key)

Registration, address settings, and the admin address editor include **Prikaži na
mapi**. Enter the street and house number; the city dropdown below is set to
**Novi Sad** (the only option). The city is appended to map queries
automatically, unless already present in the address. Click to show it
in an iframe using `https://maps.google.com/maps?q=...&output=embed`. The query is
URL-encoded, so spaces, Serbian characters, and punctuation are preserved.

This preview does not use a Maps API key, the Google Maps JavaScript SDK, or a
Geocoding API request. The former `GOOGLE_MAPS_API_KEY` setting is no longer needed
and is ignored if it remains in local settings. Google is contacted only when the
user clicks to preview the address.

The iframe is a visual aid. The app cannot read its cross-origin results, obtain
a normalized address, distinguish partial matches, or verify postal deliverability.
The user inspects the location and corrects the text if necessary; saving always
uses the address typed into the form. Editing that text or switching users in the
admin editor clears the old map so it cannot be mistaken for the new address.

Manual entry and saving work independently of the iframe, even if Google does not
render the map. JavaScript is required for the inline preview. There is no separate
external Google Maps link.

For offline browser interaction checks, run
`python tests/render_address_picker_browser.py`, then open the generated
`/tmp/psi-address-picker-test.html` in Chrome. It checks query encoding, dynamic
updates, stale-map clearing, and independent forms while intercepting iframe
navigation so no real Google requests are made.
