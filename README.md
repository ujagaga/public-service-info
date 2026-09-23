# Public Service Info

A small Flask app that warns Novi Sad residents by email when power or water
outages are announced for their address.

Users sign in with Google, save their street address, and the app mails them
whenever an announced outage for the next day covers that address.

## How it works

1. `/run_check` is triggered once a day (by cron, a web cron, or by hand) with
   the shared secret from `TRIGGER_TOKEN`.
2. `checker.py` fetches the daily power outage page (`POWER_CHECK_URL`) and
   looks for the water works announcement matching tomorrow's date on
   `WATER_CHECK_URL`.
3. For every authorized user with an address, Gemini is asked whether that
   address appears in each announcement. The pages are Cyrillic, addresses are
   typed in Latin script, so the model does the matching rather than a string
   compare.
4. Users with a hit get an email listing the service and the outage time.
5. A successful run is recorded in the `checks` table, so repeated triggers on
   the same day do nothing. A run with errors is not recorded and will retry.

## Accounts

Google OAuth handles sign-in; there are no passwords. New users pick an
address, and every admin gets an email with a one-click approval link. Users
have three levels: `0` pending, `1` approved, `2` admin. `ADMIN_EMAIL` is
seeded as the first admin when the database is created. Admins can approve,
promote, remove users and edit their addresses at `/manage_users`.

In debug mode (`application.debug`), OAuth is skipped: `/login` signs you in as
`local@localhost` with admin rights.

The UI is in Serbian.

## Files

| File | Purpose |
| --- | --- |
| `index.py` | Flask app, routes, OAuth, `/run_check` endpoint |
| `checker.py` | Fetches announcements, asks Gemini, sends notifications |
| `database.py` | SQLite schema and queries (`users`, `checks`) |
| `helper.py` | Token generation, HTTP fetch, SMTP sending, time formatting |
| `cgi_serve.py` | CGI entry point for shared hosting |
| `appsettings.py` | Configuration (not in git) |
| `client_secret.json` | Google OAuth client secrets (not in git) |

## Setup

```sh
pip install flask authlib flask-wtf httpx google-genai
cp appsettings.py.example appsettings.py   # then fill it in
```

Download the OAuth client secrets from the Google Cloud console and save them
as `client_secret.json`, with `https://<your-host>/oauth2callback` registered as
a redirect URI.

The database is created automatically on first import.

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
