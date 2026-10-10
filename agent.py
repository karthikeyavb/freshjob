#!/usr/bin/env python3
"""Fresher Job Agent: watches career pages of Indian companies and notifies you
about new fresher / intern / entry-level openings.

Usage:
    python agent.py              # normal run: fetch, filter, notify on NEW jobs
    python agent.py --verify     # check which company sources work (no notifications)
    python agent.py --dry-run    # fetch + filter, print results, don't notify or save
"""
import argparse
import json
import os
import re
import smtplib
import sys
from concurrent.futures import ThreadPoolExecutor
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
SEEN_FILE = ROOT / "seen.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (fresher-job-agent; personal use)"}
TIMEOUT = 20

# ---------------------------------------------------------------- filtering

FRESHER_TITLE = re.compile(
    r"\b(intern(ship)?s?|fresher|freshers|graduate|new grad|entry[- ]level|trainee|"
    r"campus|early career|apprentice|associate software|sde[- ]?(1|i)\b|"
    r"software (development )?engineer[- ]?(1|i)\b|engineer[- ]?(1|i)\b|"
    r"junior|jr\.?|analyst|0[- ]?(to|-)[- ]?[12] years?)",
    re.I,
)
SENIOR_TITLE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|head|vp|architect|"
    r"sde[- ]?(2|3|ii|iii)\b|engineer[- ]?(2|3|ii|iii|iv)\b|"
    r"[3-9]\+? years?|1[0-9]\+? years?)",
    re.I,
)
EXP_RANGE = re.compile(r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*\+?\s*years?", re.I)
EXP_MIN = re.compile(r"(?:minimum|at least)?\s*(\d{1,2})\s*\+\s*years?", re.I)
FRESHER_DESC = re.compile(
    r"\b(freshers?|fresh graduates?|recent graduates?|new grads?|"
    r"batch of 202[5-8]|2027 batch|2027 graduates?|0[- ]?(to|-)[- ]?[12] years?)\b",
    re.I,
)


def min_years(text):
    """Smallest 'years of experience' number mentioned, or None."""
    vals = []
    for m in EXP_RANGE.finditer(text or ""):
        vals.append(int(m.group(1)))
    for m in EXP_MIN.finditer(text or ""):
        vals.append(int(m.group(1)))
    return min(vals) if vals else None


def is_fresher_job(job, cfg):
    title = job["title"]
    desc = job.get("desc", "")
    if SENIOR_TITLE.search(title) and not re.search(r"intern", title, re.I):
        return False
    title_hit = bool(FRESHER_TITLE.search(title))
    desc_hit = bool(FRESHER_DESC.search(desc))
    yrs = min_years(desc)
    if not title_hit and not desc_hit:
        return False
    if not title_hit and yrs is not None and yrs > 1:
        return False
    # Role filter (engineering-ish) so you don't get sales/HR interns
    role_kw = [k.lower() for k in cfg.get("role_keywords", [])]
    if role_kw and not any(k in (title + " " + desc[:500]).lower() for k in role_kw):
        return False
    # Location filter
    loc_kw = [k.lower() for k in cfg.get("location_keywords", [])]
    loc = (job.get("location") or "").lower()
    if loc_kw and loc and not any(k in loc for k in loc_kw):
        return False
    return True


# ---------------------------------------------------------------- fetchers

def get(url, **kw):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def strip_html(html):
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


def fetch_greenhouse(c):
    data = get(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs?content=true").json()
    for j in data.get("jobs", []):
        yield dict(title=j["title"], url=j["absolute_url"],
                   location=(j.get("location") or {}).get("name", ""),
                   desc=strip_html(j.get("content", "")))


def fetch_lever(c):
    for j in get(f"https://api.lever.co/v0/postings/{c['slug']}?mode=json").json():
        yield dict(title=j["text"], url=j["hostedUrl"],
                   location=(j.get("categories") or {}).get("location", ""),
                   desc=j.get("descriptionPlain", ""))


def fetch_ashby(c):
    data = get(f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}").json()
    for j in data.get("jobs", []):
        yield dict(title=j["title"], url=j.get("jobUrl", ""),
                   location=j.get("location", ""),
                   desc=j.get("descriptionPlain", ""))


def fetch_smartrecruiters(c):
    data = get(f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings?limit=100").json()
    for j in data.get("content", []):
        loc = j.get("location", {})
        yield dict(title=j["name"],
                   url=f"https://jobs.smartrecruiters.com/{c['slug']}/{j['id']}",
                   location=", ".join(filter(None, [loc.get("city"), loc.get("country")])),
                   desc="")


def fetch_page(c):
    """Fallback: scrape links from a static careers page. Won't work on JS-only sites."""
    soup = BeautifulSoup(get(c["url"]).text, "html.parser")
    seen = set()
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if not (4 < len(text) < 120) or text in seen:
            continue
        seen.add(text)
        href = requests.compat.urljoin(c["url"], a["href"])
        yield dict(title=text, url=href, location="", desc="")


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "page": fetch_page,
}


def run_company(c):
    try:
        jobs = list(FETCHERS[c["ats"]](c))
        for j in jobs:
            j["company"] = c["name"]
            j["category"] = c.get("category", "")
        return c, jobs, None
    except Exception as e:  # noqa: BLE001
        return c, [], f"{type(e).__name__}: {str(e)[:100]}"


# ---------------------------------------------------------------- notify

DEFAULT_TO = "bikted29@gmail.com"


def send_email(subject, text):
    """Sends via SMTP (Gmail by default). Needs SMTP_USER and SMTP_PASS (a Gmail app password)."""
    host = os.getenv("SMTP_HOST") or "smtp.gmail.com"
    user, pwd = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS")
    to = os.getenv("EMAIL_TO") or DEFAULT_TO
    if not (user and pwd):
        return False
    msg = MIMEText(text)
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    with smtplib.SMTP_SSL(host, int(os.getenv("SMTP_PORT") or "465")) as s:
        s.login(user, pwd)
        s.send_message(msg)
    return True


def format_jobs(jobs, limit=60):
    lines = [f"🚀 {len(jobs)} new fresher opening(s)\n"]
    for j in jobs[:limit]:
        loc = f" ({j['location']})" if j["location"] else ""
        lines.append(f"• {j['company']}: {j['title']}{loc}\n  {j['url']}")
    if len(jobs) > limit:
        lines.append(f"\n…and {len(jobs) - limit} more. Check seen.json / run logs.")
    return "\n".join(lines)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "companies.yaml").read_text())
    companies = cfg["companies"]

    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(run_company, companies))

    if args.verify:
        ok = 0
        for c, jobs, err in results:
            status = f"OK  ({len(jobs)} jobs)" if not err else f"FAIL {err}"
            ok += not err
            print(f"{c['name']:<22} [{c['ats']:<15}] {status}")
        print(f"\n{ok}/{len(results)} sources working. Fix or remove the FAIL ones in companies.yaml.")
        return

    seen = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {}
    first_run = not seen
    new_jobs, matches = [], 0
    for c, jobs, err in results:
        if err:
            print(f"[warn] {c['name']}: {err}", file=sys.stderr)
        for j in jobs:
            if not is_fresher_job(j, cfg["filters"]):
                continue
            matches += 1
            if j["url"] not in seen:
                new_jobs.append(j)
                seen[j["url"]] = j["title"]

    print(f"{matches} fresher matches, {len(new_jobs)} new.")
    if args.dry_run:
        print(format_jobs(new_jobs))
        return

    if new_jobs:
        msg = format_jobs(new_jobs)
        if not send_email(f"{len(new_jobs)} new fresher jobs", msg):
            print(msg)
            print("\n[info] SMTP_USER / SMTP_PASS not set; printed instead of emailing.")
    SEEN_FILE.write_text(json.dumps(seen, indent=1))
    if first_run:
        print("First run: current matches saved. You'll only be alerted on NEW ones from now on.")


if __name__ == "__main__":
    main()
