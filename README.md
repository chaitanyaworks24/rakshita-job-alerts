# Rakshita's Job Alerts

Checks company career portals every 5 hours for **Application / Production Support, L2 Support, Support Analyst/Engineer, Incident Management and SRE** roles in **Pune** (top priority) and Bengaluru/remote India, then sends new ones to Telegram.

## Where it looks

These are the companies' own career portals (their official public job feeds):

- **Workday:** Citi, Barclays, Mastercard, Deutsche Bank, NVIDIA, FIS, Fiserv, Qualys, Accenture, Wolters Kluwer, Equifax, Hitachi, Philips, Ecolab, Zendesk, Workday, Medtronic, Red Hat
- **Greenhouse:** PubMatic, Druva, Rubrik, Sumo Logic, New Relic, Razorpay, Groww, InMobi, Okta
- **Lever:** Mindtickle, Hevo Data, Paytm, Meesho, CRED, Zeta, FamPay
- **SmartRecruiters:** Bosch, Sutherland

To add a company, add it to `config.json` (Workday companies need host/tenant/site from their careers URL).

## Alerts

- 💼 One message per new matching job, Pune first
Rakshita gets only the 💼 job alerts. Problem reports go to the maintainer's chat (`TELEGRAM_ADMIN_CHAT_ID`, which is Chaitanya):

- 📊 A daily check-in each morning (if it stops arriving, the watcher has stopped)
- ⚠️ A warning if a whole source **or a single company** (e.g. Citi blocking us) fails 2 checks in a row, and ✅ when it recovers
- ⚠️ A warning if some job alerts couldn't be delivered (the rest still go out; badly formatted ones are resent as plain text)
- 🚨 A crash alert with a link to the logs

If Telegram itself is unreachable or the bot token is revoked, the bot can't send anything, so GitHub emails the repo owner about the failed run instead.

## Run locally

```sh
python3 job_watcher.py --dry-run   # show matching jobs, send nothing
python3 job_watcher.py             # send alerts
```

`.env` holds `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. It is git-ignored; on GitHub these are repository secrets.

## Not covered automatically

Naukri, LinkedIn, Indeed, foundit and Instahyre block bots or forbid scraping. Set up their own job alerts by email instead.
