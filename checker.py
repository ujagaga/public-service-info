import datetime
import json
import logging
import re

import httpx

import appsettings
import database
import helper

logger = logging.getLogger(__name__)

OUTAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "outage": {"type": "boolean"},
        "details": {"type": "string"},
    },
    "required": ["outage", "details"],
}

API_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "gemini-3.6-flash:generateContent")


def ask(prompt: str, schema: dict | None = None):
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    if schema:
        payload["generationConfig"] = {
            "responseMimeType": "application/json",
            "responseSchema": schema,
        }

    response = httpx.post(API_URL, json=payload, timeout=60.0,
                          headers={"x-goog-api-key": appsettings.GEMINI_API_KEY})
    response.raise_for_status()
    text = response.json()["candidates"][0]["content"]["parts"][0]["text"]

    if not schema:
        return text
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, dict):
        raise ValueError(f"Neocekivan odgovor modela: {text!r}")
    return parsed


def extract_links(html: str) -> str:
    seen = {}
    for url, inner in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        if "/najava-radova-sr/" not in url or url in seen:
            continue
        seen[url] = re.sub(r"<[^>]+>", "", inner).strip()
    return "\n".join(f"{url} | {text}" for url, text in seen.items())


def find_water_page(date: datetime.date) -> str | None:
    links = extract_links(helper.fetch_html(appsettings.WATER_CHECK_URL))
    if not links:
        raise ValueError("Nijedan link objave nije pronadjen na stranici vodovoda")

    url = ask(
        f"Ispod je lista linkova sa sajta vodovoda, format 'url | tekst'. "
        f"Pronadji link objave koja se odnosi na datum {date:%d.%m.%Y}. "
        f"Odgovori samo URL-om, bez ikakvog drugog teksta. "
        f"Ako objava za taj datum ne postoji, odgovori tacno: NEMA\n\n"
        f"{links}"
    ).strip()

    if not url.startswith("http"):
        return None
    if url not in links:
        raise ValueError(f"Model je vratio link koji nije sa spiska: {url!r}")
    return helper.fetch_html(url)


def check_address(html: str, address: str, service: str):
    return ask(
        f"U HTML-u ispod je objava o planiranim prekidima u snabdevanju ({service}). "
        f"Da li za adresu {address} ima prekida? "
        f"Ako ima, u polje details upisi vreme prekida. "
        f"Adresa je pisana latinicom, a stranica cirilicom.\n\n"
        f"{html}",
        OUTAGE_SCHEMA,
    )


def run_checks() -> dict:
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    report = {"date": f"{tomorrow:%d.%m.%Y}", "checked": 0, "notified": [], "errors": [],
              "skipped": False}

    connection = database.open_db()
    try:
        if database.check_done(connection, str(today)):
            report["skipped"] = True
            return report
        users = [
            user for user in database.get_user(connection=connection)
            if user.get("authorized", 0) > 0 and user.get("address")
        ]
    finally:
        database.close_db(connection)

    if not users:
        return report

    errors = []
    pages = {}

    try:
        pages["struja"] = helper.fetch_html(appsettings.POWER_CHECK_URL)
    except Exception as error:
        errors.append(f"Preuzimanje stranice za struju nije uspelo: {error}")

    try:
        water_html = find_water_page(tomorrow)
        if water_html:
            pages["voda"] = water_html
    except Exception as error:
        errors.append(f"Preuzimanje stranice za vodu nije uspelo: {error}")

    notified = []
    for user in users:
        reports = []
        for service, html in pages.items():
            try:
                result = check_address(html, user["address"], service)
                if result["outage"]:
                    reports.append(f"{service.capitalize()}: {result['details']}")
            except Exception as error:
                errors.append(f"Provera ({service}) za {user['email']} nije uspela: {error}")

        if not reports:
            continue

        body = (
            f"Za {tomorrow:%d.%m.%Y} na adresi {user['address']} najavljeni su prekidi:\n\n"
            + "\n".join(reports)
        )
        try:
            helper.send_email(
                recipient=user["email"],
                subject=f"{appsettings.APP_TITLE}: najavljeni prekidi za {tomorrow:%d.%m.%Y}",
                body=body,
            )
            notified.append(user["email"])
        except Exception as error:
            errors.append(f"Slanje mejla za {user['email']} nije uspelo: {error}")

    if not errors:
        connection = database.open_db()
        try:
            database.record_check(connection, str(today))
        finally:
            database.close_db(connection)

    report.update({"checked": len(users), "notified": notified, "errors": errors})
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_checks())
