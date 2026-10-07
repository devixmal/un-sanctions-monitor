# UN Sanctions OSINT Monitor

A GitHub Actions job that runs every Monday, tracks **every individual and entity on the UN Security
Council Consolidated List**, sweeps open sources for new activity, and **only sends an alert when
something new and relevant turns up**. Quiet weeks produce no notification.

## What it checks each week

| Layer | Source | What triggers an alert |
|---|---|---|
| List changes | Official UN consolidated XML | New designation, delisting, or amended entry (aliases, address, notes…) |
| Global news | **GDELT bulk feed**: every article GDELT processed worldwide since the last run (≈2 million a week, English + translated from 65+ languages), matched against every name, alias and nickname. **Google News**: per-party searches in English and the party's local languages, including document numbers and original-script names | A new article about the listed party since the last run |
| Official feeds | UN SC list-update RSS + Google News topic feeds (sanctions committees, Panel of Experts, OFAC…) | Any listed name appears in a new item |
| UN reports | Panel of Experts / Monitoring Team report pages for every regime | A **new** report's full PDF text mentions any listed name |
| Official releases | UN Security Council press releases (incl. sanctions committees), US Treasury press releases, OFAC recent actions, US Justice Department (all + National Security Division), US State Department, FBI, UK OFSI | A **new** release's full text names any listed party |
| Other authorities | OpenSanctions cross-reference (OFAC, EU, UK, etc.) | A UN-listed party is newly listed elsewhere |
| Monitor health | All of the above | A source failed on ≥50% of requests, so you know coverage had a gap |

### How it keeps out irrelevant news

Every news item (GDELT, Google News, topic feeds) must pass a **context gate**: the article has to
carry a security or sanctions topic — either GDELT's own topic codes (TERROR, ARMEDCONFLICT,
ARREST, KILL, SANCTIONS…, assigned in every language) or a keyword from a multilingual list
(terror, designated, jihad, militia, arrested, sanctions, financing, smuggling… in English, French,
Arabic, Russian, Korean, Spanish, Portuguese, Turkish, Indonesian and Chinese). For heavily covered
parties (more than 15 relevant items in a run, e.g. Al-Qaida or the Houthis) only articles naming
them in the headline or opening are kept. Each alert shows the context that let it through, and the
report counts everything discarded. Edit the lists under `relevance:` in `config.yaml`.
Official releases, UN reports and document-number matches are high-signal and are not gated.

### How it avoids missing things

- **Every name variant is searched**: the primary name and *all* good-quality aliases (spread over as
  many queries as needed), plus the original-script spelling (Arabic, Korean, etc.).
- **Nicknames and short aliases** ("Tiger One", "Abu Waqas", "ADF") are searched together with the
  party's organisation acronyms and countries, e.g. `"Tiger One" AND (M23 OR Congo)`, so they find
  real coverage without flooding you with namesakes.
- **Document numbers**: passport / national-ID numbers and vessel IMO numbers are searched as well.
- **Ambiguous names need independent context**: names made only of common given names ("Abdul
  Rahman", "Hassan") are never matched on their own; short or single-word names ("Abu Anas",
  "Matiur Rahman", "Hidayatullah") and generic institutional names ("Ministry of National Defence")
  only count when the article also mentions the party's organisation, another of its names, or its
  country. A name never counts as its own context. A party whose listed names are all too common to
  search (currently one) is flagged in `data/coverage.csv`.
- **Local-language press**: besides English, each party is searched in the Google News edition for
  its countries (French for DRC/CAR/Mali/Haiti, Arabic for Yemen/Libya/Sudan/Iraq/Syria, Korean for
  DPRK, Russian, Turkish, Chinese, Spanish, Portuguese, Indonesian). GDELT adds 65+ languages.
- **No silent truncation**: when a search returns the maximum number of results, the time window is
  split and re-queried until everything is retrieved. Any limit that is still hit is listed in the report.
- **No gaps**: if the run's time budget runs out, the remaining parties are searched first next run,
  with a window reaching back to their last successful search. Parties whose searches all failed are
  flagged and retried the same way.
- **Scanned reports**: UN report PDFs without a text layer are OCR'd.
- **Nothing discarded silently**: items the filter scores below the alert threshold but above 0.3
  appear in a "Borderline — review" section.
- **Coverage audit**: each run writes `data/coverage.csv` (per party: search window, searches run,
  failures, raw results, new items, alerts, status), and the report summarises it.

Noise control: accents and "Surname, Firstname" forms are normalised, the article text is fetched so
the filter sees the actual sentence, and anything reported before is never re-sent. With an
`ANTHROPIC_API_KEY`, Claude reads each candidate against the listing profile (nationality, DOB, role,
notes) and judges whether it is really this party; without it, a keyword filter requires the name
plus a corroborating signal.

## Setup (about 10 minutes)

1. **Create a private GitHub repo** and push this folder to it:
   ```bash
   cd un-sanctions-monitor
   git remote add origin https://github.com/<you>/un-sanctions-monitor.git
   git push -u origin main
   ```
2. **Add secrets** — repo → Settings → Secrets and variables → Actions → *New repository secret*.
   All are optional; alerts always appear as a GitHub Issue (GitHub emails you about new issues).

   | Secret | Purpose |
   |---|---|
   | `ANTHROPIC_API_KEY` | Strongly recommended: AI relevance filter, far fewer false positives |
   | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `ALERT_EMAIL_TO` (`SMTP_FROM` optional) | Email the full report. For Gmail: `smtp.gmail.com`, `587`, your address, an App Password |
   | `SLACK_WEBHOOK_URL` | Short summary to a Slack channel |
   | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Short summary to Telegram |

3. **Check Actions permissions** — Settings → Actions → General → *Workflow permissions* →
   "Read and write permissions". The job commits its memory (`state/`) back to the repo.
4. **First run now** — Actions tab → *Weekly UN sanctions monitor* → *Run workflow*. Put `25` in
   *limit* for a 5-minute test, then run again with it blank for the full sweep.

The first full run stores a baseline (current list, existing reports, feed items) and alerts only on
news from the past 7 days. From then on it runs every **Monday 07:17 IST** by itself.

## Outputs

- **GitHub Issue** per week with alerts (plus email / Slack / Telegram if configured)
- `reports/YYYY-MM-DD.md` — the weekly report (top 10 items per party), kept as an audit trail
- `data/alerts/YYYY-MM-DD.csv` — every alerted and borderline item, for sorting and filtering
- `data/un_consolidated_list.csv` — the complete current list of every sanctioned individual and entity
- `state/` — the monitor's memory (list snapshot, items already seen). Don't delete it.

## Tuning (`config.yaml`)

- `list.regimes` — sweep only some regimes, e.g. `[DRC, DPRK]` (list changes are still tracked for all)
- `sources.google_news.editions` — add French / Arabic / other editions for regional press
- `sources.topic_queries` — extra news searches scanned for any listed name
- `sources.reports.pages` — add any page that links to UN reports (PDF links or `S/YYYY/NNN` symbols)
- `scoring.threshold` — raise to get fewer, higher-confidence alerts
- Change the schedule in `.github/workflows/weekly-monitor.yml` (cron is in UTC)

## Re-checking a week with new rules

Actions → *Weekly UN sanctions monitor* → *Run workflow* → mode **replay**. It re-scans the last week
of GDELT with the current configuration, sends nothing and leaves the saved state untouched; the
result is written to `logs/replay_report.md` and `logs/replay_items.csv`.

## Run locally

```bash
pip install -r requirements.txt
python -m monitor.main --limit 20 --dry-run   # prints the report, sends and saves nothing
python -m pytest -q                            # offline tests
```

## Good to know

- **Runtime**: the GDELT bulk scan takes ~5 minutes whatever the number of parties. Google News is
  paced at one search every 4 seconds; Google blocks heavy automated use, so when it does the run
  pauses 15 minutes, then stops Google News and carries the remaining parties to the next run (they
  go first, searched back to their last search). A typical run is 30 minutes to 3 hours. The
  `max_sweep_minutes` budget (270) keeps it inside GitHub's 6-hour limit; anything not reached is
  searched first the following week with no gap. Private repos on the free plan get 2,000 Actions
  minutes a month, which covers weekly runs.
- **GDELT's per-name search API** rate-limits GitHub's servers, which is why the bulk feed is used
  instead (`gdelt.doc_api` is off). Bulk files that fail to download are re-read on the next run.
- **Scheduled workflows** are paused by GitHub after 60 days without repo activity; the weekly state
  commit normally keeps the repo active. If runs stop, re-enable the workflow in the Actions tab.
- **Coverage limits**: no system can literally read the whole web. This covers worldwide news, UN
  documents, official feeds and other sanctions lists. It does not log in to social media, Telegram
  channels, paywalled sites, corporate registries, leak databases, court records or vessel-tracking
  (AIS) services; those need API keys and can be added as extra sources in `monitor/sources.py`.
- **Report pages**: the UN site uses different page paths per committee, so several are tried for
  each. If a committee shows "No report listing found" in `logs/last_run.log`, add its reports page
  under `reports.pages` in `config.yaml`.
- **Run log**: every run saves `logs/last_run.log` to the repo, with timings per step.
- **OpenSanctions licence**: free for non-commercial use (CC BY-NC 4.0). Commercial users should buy
  a licence or set `opensanctions.enabled: false`.
- Alerts are automated screening, not determinations. Verify identity before acting on any match.
