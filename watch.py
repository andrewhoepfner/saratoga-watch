#!/usr/bin/env python3
"""
17 Saratoga Ave complaint watcher.

Checks three places for complaints about 17 Saratoga Ave, Brooklyn (BIN 3039904):
  1. NYC 311 service requests (open data)
  2. DOB Complaints Received (open data)
  3. The DOB BIS complaints page (backup, since the open data can lag)

Compares against what it saw last time (state.json) and sends a phone
notification through ntfy when something is new or has changed.
"""

import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BIN = "3039904"
STATE_FILE = "state.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
DRY_RUN = os.environ.get("DRY_RUN") == "1"  # print instead of notifying
BIS_URL = ("https://a810-bisweb.nyc.gov/bisweb/ComplaintsByAddressServlet"
           f"?requestid=1&allbin={BIN}")
FAILURE_ALERT_AFTER = 4  # consecutive failed checks (= 2 days) before warning
HEADERS = {"User-Agent": "Mozilla/5.0 (17-saratoga-watch; personal monitoring)"}
NY = ZoneInfo("America/New_York")


# ---------- fetching ----------

def get(url, timeout=60):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def socrata(dataset, params):
    url = (f"https://data.cityofnewyork.us/resource/{dataset}.json?"
           + urllib.parse.urlencode(params))
    return json.loads(get(url))


def fetch_311():
    rows = socrata("erm2-nwe9", {
        "$where": ("upper(incident_address) like '17 SARATOGA%' "
                   "AND incident_zip='11233' "
                   "AND created_date > '2025-01-01T00:00:00'"),
        "$select": ("unique_key,created_date,complaint_type,descriptor,"
                    "agency,status,resolution_description,closed_date"),
        "$order": "created_date DESC",
        "$limit": 500,
    })
    items = {}
    for r in rows:
        key = r.get("unique_key")
        if not key:
            continue
        items[key] = {
            "label": f"311 #{key}",
            "date": r.get("created_date", "")[:16].replace("T", " "),
            "what": " / ".join(x for x in (r.get("complaint_type"),
                                           r.get("descriptor"),
                                           r.get("agency")) if x),
            "status": r.get("status", ""),
            "detail": r.get("resolution_description", ""),
        }
    return items


def fetch_dob_records():
    rows = socrata("eabe-havv", {"bin": BIN, "$limit": 500})
    items = {}
    for r in rows:
        key = r.get("complaint_number")
        if not key:
            continue
        bits = []
        if r.get("disposition_code"):
            bits.append(f"disposition {r['disposition_code']}")
        if r.get("disposition_date"):
            bits.append(f"on {r['disposition_date']}")
        if r.get("inspection_date"):
            bits.append(f"inspected {r['inspection_date']}")
        items[key] = {
            "label": f"DOB #{key}",
            "date": r.get("date_entered", ""),
            "what": f"category {r.get('complaint_category', '?')}",
            "status": r.get("status", ""),
            "detail": ", ".join(bits),
        }
    return items


def fetch_dob_page():
    html = get(BIS_URL)
    text = re.sub(r"<[^>]+>", " ", html).replace("&nbsp;", " ")
    if "omplaint" not in text:
        raise RuntimeError("page did not look like the DOB complaints list")
    # Brooklyn DOB complaint numbers start with 3: older ones are 7 digits
    # (3512282), newer ones have a letter (3A74795).
    numbers = set(re.findall(r"\b3(?:[A-Z]\d{5}|\d{6})\b", text)) - {BIN}
    return {n: {"label": f"DOB #{n}", "date": "",
                "what": "newly listed on the DOB BIS complaints page",
                "status": "", "detail": ""} for n in numbers}


SOURCES = [
    ("311", fetch_311),
    ("DOB records", fetch_dob_records),
    ("DOB page", fetch_dob_page),
]


# ---------- comparing ----------

def fingerprint(item):
    raw = json.dumps([item["status"], item["detail"]])
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def describe(item):
    parts = [item["label"]]
    if item["date"]:
        parts[0] += f" ({item['date']})"
    parts.append(item["what"])
    if item["status"]:
        parts.append(f"Status: {item['status']}")
    if item["detail"]:
        parts.append(item["detail"][:300])
    return " - ".join(parts)


def write_report(state, now_ny):
    """Write complaints.md: a readable list of everything the watcher sees."""
    titles = {"311": "311 complaints at 17 Saratoga (since Jan 2025)",
              "DOB records": "DOB complaints (all years)"}
    lines = ["# 17 Saratoga Ave: complaint records", "",
             f"Last checked {now_ny.strftime('%b %d, %Y %I:%M %p')} New York time. "
             "Newest first.", ""]
    for name, title in titles.items():
        items = state.get("details", {}).get(name)
        lines += [f"## {title}", ""]
        if items is None:
            lines += ["_Not reachable yet._", ""]
            continue
        lines += [f"{len(items)} on record.", "",
                  "| Number | Date | What | Status | Outcome |",
                  "|---|---|---|---|---|"]
        for item in sorted(items.values(), key=lambda i: sort_date(i["date"]),
                           reverse=True):
            cells = [item["label"].split("#")[-1], item["date"], item["what"],
                     item["status"], item["detail"]]
            lines.append("| " + " | ".join(
                c.replace("|", "/").replace("\n", " ") for c in cells) + " |")
        lines.append("")
    with open("complaints.md", "w") as f:
        f.write("\n".join(lines))


def sort_date(d):
    # 311 dates are "YYYY-MM-DD HH:MM"; DOB dates are "MM/DD/YYYY".
    m = re.match(r"(\d\d)/(\d\d)/(\d{4})", d or "")
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else (d or "")


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)


# ---------- notifying ----------

def notify(title, body, priority="default", tags="house"):
    if DRY_RUN or not NTFY_TOPIC:
        print(f"\n[notification] {title}\n{body}\n")
        if not NTFY_TOPIC and not DRY_RUN:
            print("NTFY_TOPIC is not set, so nothing was sent to your phone.")
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Tags": tags,
                 "Click": BIS_URL},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


# ---------- main ----------

# Two ways a check gets started:
#  1. The outside timer (cron-job.org) at exactly 9:30am/pm, which passes
#     CHECK=morning or CHECK=evening.
#  2. GitHub's own schedule, kept as a backup. It's often hours late, so it
#     stands down if the timer already did that check.
# GitHub's schedule runs on UTC, so it lists summer and winter versions.
SCHEDULES = {
    "30 13 * * *": ("morning", True),   # 9:30am New York, daylight time
    "30 1 * * *":  ("evening", True),   # 9:30pm New York, daylight time
    "30 14 * * *": ("morning", False),  # 9:30am New York, standard time
    "30 2 * * *":  ("evening", False),  # 9:30pm New York, standard time
}


def which_check():
    """Returns ('morning'|'evening'|'manual', from_backup) or None to skip."""
    requested = os.environ.get("CHECK", "").strip().lower()
    if requested in ("morning", "evening", "manual"):
        return requested, False
    schedule = os.environ.get("CRON_SCHEDULE", "")
    if schedule not in SCHEDULES:
        return "manual", False
    slot, for_summer = SCHEDULES[schedule]
    summer = datetime.now(timezone.utc).astimezone(NY).dst().total_seconds() > 0
    return (slot, True) if for_summer == summer else None


def slot_id(check, now_ny):
    """Which day's morning/evening check this is. A late evening check that
    runs after midnight still belongs to the previous evening."""
    day = now_ny.date()
    if check == "evening" and now_ny.hour < 12:
        day -= timedelta(days=1)
    return f"{day.isoformat()} {check}"


def main():
    which = which_check()
    if which is None:
        print("Off-season backup schedule; skipping this run.")
        return
    check, from_backup = which
    state = load_state()
    now_ny = datetime.now(timezone.utc).astimezone(NY)
    done = state.setdefault("slots_done", [])
    if check in ("morning", "evening"):
        slot = slot_id(check, now_ny)
        if from_backup and slot in done:
            print(f"The timer already ran the {slot} check; backup standing down.")
            return
        if slot not in done:
            done.append(slot)
        del done[:-20]  # keep the list short
    print(f"Running the {check} check" + (" (backup)." if from_backup else "."))
    state.setdefault("sources", {})
    state.setdefault("failures", {})

    changes, problems, baseline = [], [], []

    for name, fetch in SOURCES:
        try:
            items = fetch()
        except Exception as e:  # network trouble, site down, etc.
            n = state["failures"].get(name, 0) + 1
            state["failures"][name] = n
            print(f"{name}: check failed ({n} in a row): {e}")
            if n == FAILURE_ALERT_AFTER:
                problems.append(f"Couldn't reach {name} for the last {n} "
                                "checks. Worth a manual look.")
            continue

        if state["failures"].get(name, 0) >= FAILURE_ALERT_AFTER:
            problems.append(f"{name} is reachable again.")
        state["failures"][name] = 0
        state.setdefault("details", {})[name] = items

        seen = state["sources"].get(name)
        if seen is None:  # first time checking this source: just record it
            state["sources"][name] = {k: fingerprint(v) for k, v in items.items()}
            baseline.append(f"{name}: {len(items)} complaint(s) on record")
            continue

        # Records that vanished from the city's data (usually a glitch while
        # the city reloads it). Tell once when it starts and once when fixed.
        missing_before = set(state.setdefault("missing", {}).get(name, []))
        missing_now = {k for k in seen if k not in items}
        if missing_now and missing_now != missing_before:
            labels = ", ".join(sorted(missing_now))
            problems.append(
                f"{name}: {len(missing_now)} complaint(s) we saw before are "
                f"missing from the city's data now ({labels}). Usually the city "
                "is mid-update and they reappear; worth a glance at BIS.")
        elif missing_before and not missing_now:
            problems.append(f"{name}: all previously seen complaints are back "
                            "in the city's data.")
        state["missing"][name] = sorted(missing_now)

        for key, item in sorted(items.items(), key=lambda kv: kv[1]["date"],
                                reverse=True):
            fp = fingerprint(item)
            if key not in seen:
                changes.append("NEW: " + describe(item))
            elif seen[key] != fp:
                changes.append("UPDATED: " + describe(item))
            seen[key] = fp
        print(f"{name}: {len(items)} on record, checked OK")

    if changes:
        notify(f"17 Saratoga: {len(changes)} new or changed complaint record(s)",
               "\n\n".join(changes), priority="high", tags="rotating_light")
    if problems:
        notify("17 Saratoga watcher: data note", "\n\n".join(problems),
               tags="warning")
    if baseline:
        notify("17 Saratoga watcher is running",
               "Starting point recorded. You'll hear from me when anything "
               "new appears.\n\n" + "\n".join(baseline), tags="white_check_mark")

    # Reassurance after each morning and evening check, so silence never
    # means "broken". Sent once per check, even if a backup run repeats it.
    if check in ("morning", "evening") and not changes and not baseline:
        slot = slot_id(check, now_ny)
        if state.get("last_heartbeat") != slot:
            counts = ", ".join(f"{name}: {len(items)}" for name, items
                               in state.get("details", {}).items()
                               if name != "DOB page")
            notify(f"17 Saratoga: all quiet ({check})",
                   f"No new or changed complaints. On record: {counts}.",
                   priority="low", tags="zzz")
            state["last_heartbeat"] = slot

    state["last_checked"] = now_ny.isoformat(timespec="minutes")
    save_state(state)
    write_report(state, now_ny)


if __name__ == "__main__":
    main()
