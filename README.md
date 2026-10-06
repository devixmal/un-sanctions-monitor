# UN Sanctions OSINT Monitor

A GitHub Actions job that runs every Monday, tracks **every individual and entity on the UN Security
Council Consolidated List**, sweeps open sources for new activity, and **only sends an alert when
something new and relevant turns up**. Quiet weeks produce no notification.

## What it checks each week

| Layer | Source | What triggers an alert |
|---|---|---|
| List changes | Official UN consolidated XML | New designation, delisting, or amended entry (aliases, address, notes…) |
| Global news | GDELT (65+ languages, translated) + Google News, every name and good-quality alias, plus original-script names | A new article about the listed party in the last 7 days |
| Official feeds | UN SC list-update RSS + Google News topic feeds (sanctions committees, Panel of Experts, OFAC…) | Any listed name appears in a new item |
| UN reports | Panel of Experts / Monitoring Team report pages for every regime | A **new** report's full PDF text mentions any listed name |
| Other authorities | OpenSanctions cross-reference (OFAC, EU, UK, etc.) | A UN-listed party is newly listed elsewhere |
| Monitor health | All of the above | A source failed on ≥50% of requests, so you know coverage had a gap |

Noise control: names shorter than 8 characters and low-quality aliases are not searched, accents and
"Surname, Firstname" forms are normalised, the article text is fetched so the filter sees the actual
sentence, and anything reported before is never re-sent. With an `ANTHROPIC_API_KEY`, Claude reads
each candidate against the listing profile (nationality, DOB, role, notes) and drops namesakes;
without it, a keyword filter requires the name plus a corroborating signal.

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
- `reports/YYYY-MM-DD.md` — the full weekly report, kept in the repo as an audit trail
- `data/un_consolidated_list.csv` — the complete current list of every sanctioned individual and entity
- `state/` — the monitor's memory (list snapshot, items already seen). Don't delete it.

## Tuning (`config.yaml`)

- `list.regimes` — sweep only some regimes, e.g. `[DRC, DPRK]` (list changes are still tracked for all)
- `sources.google_news.editions` — add French / Arabic / other editions for regional press
- `sources.topic_queries` — extra news searches scanned for any listed name
- `sources.reports.pages` — add any page that links to UN reports (PDF links or `S/YYYY/NNN` symbols)
- `scoring.threshold` — raise to get fewer, higher-confidence alerts
- Change the schedule in `.github/workflows/weekly-monitor.yml` (cron is in UTC)

## Run locally

```bash
pip install -r requirements.txt
python -m monitor.main --limit 20 --dry-run   # prints the report, sends and saves nothing
python -m pytest -q                            # offline tests
```

## Good to know

- **Runtime**: a full sweep of ~1,000 parties takes roughly 2–3 hours (GDELT allows one query
  every ~5 seconds). That is within GitHub's 6-hour job limit. Public repos have unlimited Actions
  minutes; private repos on the free plan get 2,000 minutes a month, which covers weekly runs.
- **Scheduled workflows** are paused by GitHub after 60 days without repo activity; the weekly state
  commit normally keeps the repo active. If runs stop, re-enable the workflow in the Actions tab.
- **Coverage limits**: no system can literally read the whole web. This covers worldwide news, UN
  documents, official feeds and other sanctions lists. It does not log in to social media, Telegram
  channels, paywalled sites, corporate registries or vessel-tracking (AIS) services; those need paid
  APIs and can be added as extra sources in `monitor/sources.py`.
- **Report pages**: the 1267 and 1988 monitoring-team page addresses follow the UN site's pattern but
  may differ; after the first run, check the job log for `Report page … failed` and fix any URL.
- **OpenSanctions licence**: free for non-commercial use (CC BY-NC 4.0). Commercial users should buy
  a licence or set `opensanctions.enabled: false`.
- Alerts are automated screening, not determinations. Verify identity before acting on any match.
