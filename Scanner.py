#!/usr/bin/env python3
"""
Recreation.gov campsite availability scanner.

Multi-user mode: reads two published-to-web CSV tabs from a Google Sheet --
a "Campgrounds" catalog (state, campground_name, facility_id, url) and a
"Signups" tab fed by a Google Form (name, email, state, campgrounds,
start_date, end_date, alert_mode, unsubscribe_token, active). Checks
recreation.gov's (unofficial) availability API once per unique campground,
compares against state.json from the last run, and emails each watcher
their own newly-available sites via Resend.

Designed to be run on a schedule (see .github/workflows/scan.yml) rather
than as a long-running process -- each run is a single, cheap pass.
"""

import csv
import io
import json
import os
import sys
import time
from datetime import date, datetime

import requests

STATE_PATH = "state.json"
API_URL = "https://www.recreation.gov/api/camps/availability/campground/{facility_id}/month"
RESEND_API_URL = "https://api.resend.com/emails"

# Safety cap so a handful of signups can't blow through Resend's free
# daily quota. Enforced here regardless of what the Form allows, in case
# someone submits more than once.
MAX_CAMPGROUNDS_PER_PERSON = 2

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


def fetch_csv(url):
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return list(csv.DictReader(io.StringIO(resp.text)))


def load_campgrounds(csv_url):
    """Returns {(state, campground_name): {"facility_id": ..., "url": ...}}."""
    catalog = {}
    for row in fetch_csv(csv_url):
        state = (row.get("state") or "").strip()
        name = (row.get("campground_name") or "").strip()
        facility_id = (row.get("facility_id") or "").strip()
        if not state or not name or not facility_id:
            continue
        catalog[(state, name)] = {
            "facility_id": facility_id,
            "url": (row.get("url") or "").strip(),
        }
    return catalog


def load_watchers(csv_url, catalog):
    """Parse Form signups into a list of watchers, resolving each selected
    campground against the catalog, skipping inactive/unresolvable rows,
    and capping each person at MAX_CAMPGROUNDS_PER_PERSON campgrounds
    total across all of their rows."""
    today = date.today().isoformat()
    by_email = {}

    for row in fetch_csv(csv_url):
        if (row.get("active") or "TRUE").strip().upper() != "TRUE":
            continue

        email = (row.get("email") or "").strip().lower()
        start_date = (row.get("start_date") or "").strip()
        end_date = (row.get("end_date") or "").strip()
        alert_mode = (row.get("alert_mode") or "").strip().lower()
        if not email or not start_date or not end_date:
            continue

        # Belt-and-suspenders: Apps Script auto-expires limited-window
        # rows daily, but skip stale ones here too in case that hasn't
        # run yet.
        if "rolling" not in alert_mode and end_date < today:
            continue

        state_name = (row.get("state") or "").strip()
        selections = [c.strip() for c in (row.get("campgrounds") or "").split(",") if c.strip()]

        resolved = []
        for name in selections:
            info = catalog.get((state_name, name))
            if not info:
                print(f"  WARNING: unknown campground '{name}' ({state_name}) for {email}, skipping")
                continue
            resolved.append({"name": name, "facility_id": info["facility_id"], "url": info["url"]})

        if not resolved:
            continue

        by_email.setdefault(email, []).append({
            "name": (row.get("name") or "").strip(),
            "email": email,
            "start_date": start_date,
            "end_date": end_date,
            "token": (row.get("unsubscribe_token") or "").strip(),
            "campgrounds": resolved,
        })

    watchers = []
    for email, entries in by_email.items():
        remaining = MAX_CAMPGROUNDS_PER_PERSON
        for entry in entries:
            if remaining <= 0:
                print(f"  WARNING: {email} exceeds {MAX_CAMPGROUNDS_PER_PERSON}-campground cap, ignoring extra signups")
                break
            entry["campgrounds"] = entry["campgrounds"][:remaining]
            remaining -= len(entry["campgrounds"])
            watchers.append(entry)

    return watchers


def month_start_dates(start_date_str, end_date_str):
    """Yield the first-of-month date for every month the range touches,
    since the API only accepts one calendar month at a time."""
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


def available_sites_for_facility(session, facility_id, start_date, end_date):
    """Return a set of 'site|date' strings currently available at this
    facility within the given date range."""
    found = set()
    for month_start in month_start_dates(start_date, end_date):
        data = fetch_month(session, facility_id, month_start)
        if not data:
            continue
        for site_id, site_info in data.get("campsites", {}).items():
            site_label = site_info.get("site", site_id)
            for date_str, status in (site_info.get("availabilities") or {}).items():
                day = date_str[:10]  # "2026-09-05T00:00:00Z" -> "2026-09-05"
                if day < start_date or day > end_date:
                    continue
                if status in AVAILABLE_STATUSES:
                    found.add(f"{site_label}|{day}")
    return found


def send_alert_email(api_key, from_email, unsubscribe_base_url, watcher, openings):
    lines = []
    for name, url, site, day in openings:
        suffix = f"  ->  {url}" if url else ""
        lines.append(f"- {name}: site {site} on {day}{suffix}")

    unsubscribe_link = f"{unsubscribe_base_url}?token={watcher['token']}"
    greeting = f"Hi {watcher['name']}," if watcher["name"] else "Hi,"
    body = (
        f"{greeting}\n\n"
        "New campsite openings found:\n\n" + "\n".join(lines) +
        f"\n\nDon't want these alerts anymore? Unsubscribe: {unsubscribe_link}\n"
    )
    subject = f"Campsite alert: {len(openings)} new opening(s)"

    resp = requests.post(
        RESEND_API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"from": from_email, "to": [watcher["email"]], "subject": subject, "text": body},
        timeout=15,
    )
    if resp.status_code >= 300:
        print(f"  ERROR sending to {watcher['email']}: {resp.status_code} {resp.text}")
    else:
        print(f"  Sent alert to {watcher['email']} ({len(openings)} opening(s))")


def main():
    campgrounds_csv_url = os.environ.get("CAMPGROUNDS_CSV_URL")
    signups_csv_url = os.environ.get("SIGNUPS_CSV_URL")
    resend_api_key = os.environ.get("RESEND_API_KEY")
    from_email = os.environ.get("FROM_EMAIL")
    unsubscribe_base_url = os.environ.get("UNSUBSCRIBE_BASE_URL")

    missing = [
        var for var, val in [
            ("CAMPGROUNDS_CSV_URL", campgrounds_csv_url),
            ("SIGNUPS_CSV_URL", signups_csv_url),
            ("RESEND_API_KEY", resend_api_key),
            ("FROM_EMAIL", from_email),
            ("UNSUBSCRIBE_BASE_URL", unsubscribe_base_url),
        ] if not val
    ]
    if missing:
        print(f"ERROR: missing required environment variable(s): {', '.join(missing)}")
        sys.exit(1)

    catalog = load_campgrounds(campgrounds_csv_url)
    watchers = load_watchers(signups_csv_url, catalog)

    if not watchers:
        print("No active watchers -- nothing to do.")
        return

    # Fetch each unique campground once, over the union of every watcher's
    # date range for it, rather than once per person.
    facility_range = {}
    for w in watchers:
        for cg in w["campgrounds"]:
            fid = cg["facility_id"]
            lo, hi = facility_range.get(fid, (w["start_date"], w["end_date"]))
            facility_range[fid] = (min(lo, w["start_date"]), max(hi, w["end_date"]))

    state = load_json(STATE_PATH, {})
    new_state = {}
    current_by_facility = {}
    session = requests.Session()

    for fid, (start_date, end_date) in facility_range.items():
        print(f"Checking facility {fid} {start_date}..{end_date}")
        current_by_facility[fid] = available_sites_for_facility(session, fid, start_date, end_date)
        new_state[fid] = sorted(current_by_facility[fid])
        time.sleep(1)  # be polite between campgrounds

    save_json(STATE_PATH, new_state)
    previous_by_facility = {fid: set(state.get(fid, [])) for fid in facility_range}

    emails_sent = 0
    for w in watchers:
        openings = []
        for cg in w["campgrounds"]:
            current = current_by_facility.get(cg["facility_id"], set())
            previous = previous_by_facility.get(cg["facility_id"], set())
            for entry in sorted(current - previous):
                site, day = entry.split("|", 1)
                if w["start_date"] <= day <= w["end_date"]:
                    openings.append((cg["name"], cg["url"], site, day))

        if not openings:
            continue
        send_alert_email(resend_api_key, from_email, unsubscribe_base_url, w, openings)
        emails_sent += 1
        time.sleep(1)  # be polite to Resend between sends

    print(f"Done. {emails_sent} alert email(s) sent. ({datetime.utcnow().isoformat()}Z)")


if __name__ == "__main__":
    main()
