# Fresher Job Agent

Checks career pages of ~190 Indian startups, product, fintech and AI companies (the lists from your five PDFs, de-duplicated, plus a few extras) every 6 hours and
emails you at bikted29@gmail.com when a **new** fresher / intern / entry-level engineering
role appears. Runs free on GitHub Actions, so your laptop doesn't need to be on.

## Setup (about 10 minutes)

1. **Create a GitHub repo** (private is fine) and upload everything in this folder, including the
   `.github/workflows/check.yml` file.
2. **Gmail app password** (the account that *sends* the alerts; it can be bikted29@gmail.com itself):
   - Turn on 2-Step Verification for that Google account.
   - Go to https://myaccount.google.com/apppasswords, create an app password, copy the 16 characters.
3. In the repo: **Settings → Secrets and variables → Actions → New repository secret**. Add
   `SMTP_USER` (the sending Gmail address) and `SMTP_PASS` (the app password).
   Alerts are emailed to **bikted29@gmail.com** by default; add an `EMAIL_TO` secret to change it.
   Check Spam the first time and mark the email "Not spam".
4. **Actions tab → fresher-job-agent → Run workflow** to trigger the first run. The first run emails you every
   fresher opening that is open right now, plus a list of companies it couldn't read; after that you only get
   emails for new postings.

## How companies are found

Your PDFs only give company names, so most entries are `ats: auto`: on the first run the agent tries each
company's likely job-board name on Greenhouse, Lever, Ashby and SmartRecruiters, and remembers what it
finds in `sources_cache.json` (retrying misses weekly). Companies with no public board of those four
(many big ones use Workday or their own site) show up in the first email under "Not readable yet".
For those, add `ats: page` + `url:` (weak) or the right `slug` in `companies.yaml`.
Auto-discovery can occasionally match a look-alike board, so skim `sources_cache.json` once.

## Check which companies actually work (important)

Company slugs and URLs in `companies.yaml` are best guesses. Locally run:

```
pip install -r requirements.txt
python agent.py --verify
```

- `OK (N jobs)`: working.
- `FAIL`: wrong slug or URL. Fix it, or delete the line.
- `[page]` entries are a weak fallback: they only work on static HTML careers pages. Most big
  companies use JavaScript-rendered sites (Workday, custom portals) and will show 0 jobs. For those,
  open the careers page, click a job, and check the URL: if it's on `boards.greenhouse.io`,
  `jobs.lever.co`, `jobs.ashbyhq.com` or `jobs.smartrecruiters.com`, switch the entry to that `ats`
  with the slug from the URL. That is far more reliable.

## Tuning

- `filters.role_keywords` / `location_keywords` in `companies.yaml` control what counts as a match.
- Fresher detection (`agent.py`): title keywords (intern, graduate, SDE-1, trainee, ...), or
  "0-2 years" / "freshers" in the description. Senior titles and "3+ years" are excluded.
- `python agent.py --dry-run` prints matches without notifying or saving.

## Limits

- Companies without a public ATS API (many large ones) can't be tracked reliably by this approach.
- Don't make the schedule much more frequent than hourly; it's polite to career sites.
