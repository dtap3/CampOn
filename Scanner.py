#!/usr/bin/env python3
"""
Recreation.gov campsite availability scanner.

Reads config.json for a list of campgrounds/date ranges to watch, checks
recreation.gov's (unofficial) availability API, compares the result against
state.json from the last run, and emails you when a site that was NOT
available last time becomes available.

Designed to be run on a schedule (see .github/workflows/scan.yml) rather
than as a long-running process -- each run is a single, cheap pass.
"""

import json
import os
import smtplib
import sys
import time
from datetime import date, datetime
from email.mime.text import MIMEText

import requests

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"
API_URL = "https://www.recreation.gov/api/camps/availability/campground/{facility_id}/month"

# Recreation.gov blocks requests that look like bots. A normal browser
# User-Agent avoids most of that without doing anything sneaky.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

AVAILABLE_STATUSES = {"Available"}


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def month_start_dates(start_date_str, end_date_str):
    """Yield the first-of-month date (as YYYY-MM-01) for every month the
    range touches, since the API only accepts one calendar month at a time."""
    start = date.fromisoformat(start_date_str)
    end = date.fromisoformat(end_date_str)
    current = start.replace(day=1)
    while current <= end:
        yield current
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)


def fetch_month(session, facility_id, month_start, retries=3):
    params = {"start_date": f"{month_start.isoformat()}T00:00:00.000Z"}
    url = API_URL.format(facility_id=facility_id)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, params=params, headers=HEADERS, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            last_err = f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            last_err = str(exc)
        time.sleep(2 * attempt)  # simple backoff
    print(f"  WARNING: failed to fetch {facility_id} for {month_start}: {last_err}")
    return None


def available_sites_for_campground(session, cg):
    """Return a set of 'site|date' strings currently available for this
    campground within its configured date range."""
    facility_id = cg["facility_id"]
    start_date = cg["start_date"]
    end_date = cg["end_date"]
    site_filter = set(cg.get("site_ids") or [])

    found = set()
    for month_start in month_start_dates(start_date, end_date):
        data = fetch_month(session, facility_id, month_start)
        if not data:
            continue
        campsites = data.get("campsites", {})
        for site_id, site_info in campsites.items():
            if site_filter and site_id not in site_filter:
                continue
            site_label = site_info.get("site", site_id)
            for date_str, status in (site_info.get("availabilities") or {}).items():
                day = date_str[:10]  # "2026-09-05T00:00:00Z" -> "2026-09-05"
                if day < start_date or day > end_date:
                    continue
                if status in AVAILABLE_STATUSES:
                    found.add(f"{site_label}|{day}")
    return found


def send_email(to_email, subject, body):
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_email

    with smtplib.SMTP_SSL("smtp.mail.yahoo.com", 465) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [to_email], msg.as_string())


def main():
    config = load_json(CONFIG_PATH, {"campgrounds": []})
    state = load_json(STATE_PATH, {})
    to_email = os.environ.get("TO_EMAIL")

    if not config.get("campgrounds"):
        print("No campgrounds configured in config.json -- nothing to do.")
        return

    session = requests.Session()
    new_state = {}
    all_new_openings = []  # (campground_name, facility_id, site, date)

    for cg in config["campgrounds"]:
        name = cg.get("name", cg["facility_id"])
        print(f"Checking {name} ({cg['facility_id']}) {cg['start_date']}..{cg['end_date']}")
        current = available_sites_for_campground(session, cg)
        previous = set(state.get(cg["facility_id"], []))

        newly_available = current - previous
        for entry in sorted(newly_available):
            site, day = entry.split("|", 1)
            all_new_openings.append((name, cg["facility_id"], site, day))

        new_state[cg["facility_id"]] = sorted(current)
        time.sleep(1)  # be polite between campgrounds

    save_json(STATE_PATH, new_state)

    if not all_new_openings:
        print(f"No new openings. ({datetime.utcnow().isoformat()}Z)")
        return

    print(f"Found {len(all_new_openings)} new opening(s)! Sending email...")
    lines = []
    for name, facility_id, site, day in all_new_openings:
        link = f"https://www.recreation.gov/camping/campgrounds/{facility_id}"
        lines.append(f"- {name}: site {site} on {day}  ->  {link}")

    body = "New campsite openings found:\n\n" + "\n".join(lines) + "\n"
    subject = f"Campsite alert: {len(all_new_openings)} new opening(s)"

    if not to_email:
        print("WARNING: TO_EMAIL not set, skipping email. Openings:\n" + body)
        return

    try:
        send_email(to_email, subject, body)
        print("Email sent.")
    except Exception as exc:
        print(f"ERROR sending email: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
