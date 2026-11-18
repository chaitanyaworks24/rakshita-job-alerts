#!/usr/bin/env python3
"""Job watcher for Rakshita: checks company career portals (Workday, Greenhouse,
Lever, SmartRecruiters) for application/production support roles in Pune
(and Bengaluru/remote India), and sends new ones to Telegram.

Usage:
  python3 job_watcher.py                  # normal run (send alerts)
  python3 job_watcher.py --dry-run        # print jobs, send nothing, don't save state
  python3 job_watcher.py --test-telegram  # send a test message
  python3 job_watcher.py --get-chat-id    # list chats that have messaged the bot

Env vars (or a .env file next to this script): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import argparse
import concurrent.futures
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
SEEN_PATH = os.path.join(HERE, "seen.json")
HEALTH_PATH = os.path.join(HERE, "health.json")
USER_AGENT = "Mozilla/5.0 (Macintosh) job-watcher/1.0"
SEEN_RETENTION_DAYS = 60
FAIL_ALERT_AFTER = 2  # consecutive failed checks (~10 hours) before warning on Telegram
PARTIAL_ERRORS = {}  # "source/company" -> error, filled by fetchers when one company or board fails


# ---------- helpers ----------

def load_dotenv():
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def log(msg):
    print(msg, file=sys.stderr)


def http_get(url, body=None, retries=2, headers=None):
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, data=json.dumps(body).encode() if body is not None else None,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json", "Content-Type": "application/json",
                         **(headers or {})})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise last_err


def http_json(url, body=None, retries=2, headers=None):
    return json.loads(http_get(url, body, retries, headers))


def parse_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        ts = float(value)
        return datetime.fromtimestamp(ts / 1000 if ts > 1e11 else ts, timezone.utc)
    value = str(value).strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_workday_posted(text, now):
    # "Posted Today", "Posted Yesterday", "Posted 3 Days Ago", "Posted 30+ Days Ago"
    text = (text or "").lower()
    if "today" in text:
        return now
    if "yesterday" in text:
        return now - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*day", text)
    return now - timedelta(days=int(m.group(1))) if m else None


def job(source, company, job_id, title, url, location, posted):
    return {"key": "%s:%s:%s" % (source, company, job_id), "source": source, "company": (company or "").strip(),
            "title": html.unescape(title or "").strip(), "url": url, "location": (location or "").strip(),
            "posted": posted}


# ---------- sources ----------

# ---------- Indian job boards ----------

def fetch_gladiator(source, host, cfg):
    """iimjobs and Hirist run on the same backend ("gladiator") with a JSON search API."""
    jobs = []
    for q in cfg["queries"]:
        for page in range(1, cfg.get("max_pages", 3) + 1):
            time.sleep(cfg.get("delay_seconds", 10))  # both sites ask for Crawl-delay: 10 in robots.txt
            url = "https://%s/job/search?%s" % (host, urllib.parse.urlencode(
                {"query": q, "page": page, "posting": cfg.get("posted_within_days", 3)}))
            try:
                data = http_json(url, headers={"version": "2"})
            except Exception as e:
                log("%s %r failed: %s" % (source, q, e))
                PARTIAL_ERRORS["%s/%s" % (source, q)] = str(e)
                break
            for j in data.get("data", []):
                location = "; ".join(l.get("name", "") for l in j.get("locations") or [])
                if j.get("workFromHome"):
                    location = "Remote, India; " + location
                # jobdesignation is the title without the "Company - " prefix iimjobs adds
                jobs.append(job(source, (j.get("companyData") or {}).get("companyName"), j["id"],
                                j.get("jobdesignation") or j.get("title"), j.get("jobDetailUrl"), location,
                                parse_date(j.get("createdTimeMs") or j.get("createdTime"))))
            if not data.get("hasMore"):
                break
    return jobs


def fetch_jobspy(site):
    """LinkedIn, Naukri, Indeed and Glassdoor via the JobSpy library (pip install python-jobspy; Python 3.10+)."""
    def fetch(cfg, config, now):
        try:
            from jobspy import scrape_jobs
        except ImportError:
            raise RuntimeError("python-jobspy is not installed (needs Python 3.10+: pip install -r requirements.txt)")
        jobs, failed = [], []
        for location in cfg["locations"]:
            for q in cfg["queries"]:
                try:
                    df = scrape_jobs(site_name=[site], search_term=q, location=location, country_indeed="india",
                                     results_wanted=cfg.get("results_wanted", 30),
                                     hours_old=cfg.get("hours_old", 24), verbose=0)
                except Exception as e:
                    log("%s %r in %s failed: %s" % (site, q, location, e))
                    failed.append("%s in %s: %s" % (q, location, e))
                    continue
                for r in df.to_dict("records"):
                    r = {k: (None if v != v else v) for k, v in r.items()}  # pandas uses NaN for missing values
                    place = str(r.get("location") or "")
                    city = location.split(",")[0]
                    if city.lower() != "india" and city.lower() not in place.lower():
                        place = "%s; %s" % (city, place)  # Indeed only gives the state ("KA, India")
                    if r.get("is_remote"):
                        place = "Remote, India" + ("" if place.lower() in ("", "india") else "; " + place)
                    jobs.append(job(site, r.get("company"), r.get("id") or r.get("job_url"), r.get("title"),
                                    r.get("job_url"), place, parse_date(str(r["date_posted"])) if r.get("date_posted") else None))
                time.sleep(cfg.get("delay_seconds", 3))
        total = len(cfg["locations"]) * len(cfg["queries"])
        if len(failed) == total:
            raise RuntimeError("every %s search failed: %s" % (site, failed[0]))
        if failed:  # LinkedIn often rate-limits a few searches from GitHub: only warn when they all fail (above)
            log("%s: %d of %d searches failed" % (site, len(failed), total))
        return jobs
    return fetch


def fetch_iimjobs(cfg, config, now):
    return fetch_gladiator("iimjobs", "gladiator.iimjobs.com", cfg)


def fetch_hirist(cfg, config, now):
    return fetch_gladiator("hirist", "gladiator.hirist.tech", cfg)


# ---------- company career portals ----------

def fetch_workday(cfg, config, now):
    def base(c):
        return "https://%s.myworkdayjobs.com/wday/cxs/%s/%s" % (c["host"], c["tenant"], c["site"])

    def one(c, query, city):
        data = http_json(base(c) + "/jobs", {"appliedFacets": {}, "limit": 20, "offset": 0,
                                              "searchText": "%s %s" % (query, city)})
        found = []
        for p in data.get("jobPostings", []):
            if not p.get("externalPath"):
                continue
            location = p.get("locationsText") or ""
            if re.search(r"\d+\s+locations", location, re.I):
                # Multi-city posting: look up the real list of cities
                info = http_json(base(c) + p["externalPath"]).get("jobPostingInfo", {})
                location = "; ".join([info.get("location") or ""] + (info.get("additionalLocations") or []))
            found.append(job("workday", c["name"], p["externalPath"], p["title"],
                             "https://%s.myworkdayjobs.com/%s%s" % (c["host"], c["site"], p["externalPath"]),
                             location, parse_workday_posted(p.get("postedOn"), now)))
        return found

    tasks = [(c, q, city) for c in cfg["companies"] for q in config["search_queries"] for city in cfg["cities"]]
    jobs, errors = [], {}
    with concurrent.futures.ThreadPoolExecutor(24) as pool:
        futures = {pool.submit(one, *t): t[0]["name"] for t in tasks}
        for f in concurrent.futures.as_completed(futures):
            try:
                jobs.extend(f.result())
            except Exception as e:
                errors[futures[f]] = str(e)
                PARTIAL_ERRORS["workday/" + futures[f]] = str(e)
    if errors:
        log("workday errors: %s" % errors)
    if errors and len(errors) == len(cfg["companies"]):
        raise RuntimeError("all Workday companies failed")
    return jobs


def fetch_greenhouse(cfg, config, now):
    jobs = []
    for board in cfg["boards"]:
        try:
            data = http_json("https://boards-api.greenhouse.io/v1/boards/%s/jobs" % board)
        except Exception as e:
            log("greenhouse %s failed: %s" % (board, e))
            PARTIAL_ERRORS["greenhouse/" + board] = str(e)
            continue
        for j in data.get("jobs", []):
            jobs.append(job("greenhouse", board, j["id"], j["title"], j["absolute_url"],
                            (j.get("location") or {}).get("name"),
                            parse_iso(j.get("first_published") or j.get("updated_at"))))
    return jobs


def fetch_lever(cfg, config, now):
    jobs = []
    for company in cfg["companies"]:
        try:
            data = http_json("https://api.lever.co/v0/postings/%s?mode=json" % company)
        except Exception as e:
            log("lever %s failed: %s" % (company, e))
            PARTIAL_ERRORS["lever/" + company] = str(e)
            continue
        for j in data:
            cats = j.get("categories") or {}
            location = ", ".join(cats.get("allLocations") or [cats.get("location") or ""])
            created = j.get("createdAt")
            jobs.append(job("lever", company, j["id"], j["text"], j["hostedUrl"], location,
                            datetime.fromtimestamp(created / 1000, timezone.utc) if created else None))
    return jobs


def fetch_smartrecruiters(cfg, config, now):
    jobs = []
    for company in cfg["companies"]:
        for city in cfg["cities"]:
            try:
                data = http_json("https://api.smartrecruiters.com/v1/companies/%s/postings?limit=100&city=%s"
                                 % (company, city))
            except Exception as e:
                log("smartrecruiters %s/%s failed: %s" % (company, city, e))
                PARTIAL_ERRORS["smartrecruiters/" + company] = str(e)
                continue
            for j in data.get("content", []):
                loc = j.get("location") or {}
                jobs.append(job("smartrecruiters", (j.get("company") or {}).get("name", company), j["id"], j["name"],
                                "https://jobs.smartrecruiters.com/%s/%s" % (company, j["id"]),
                                ", ".join(x for x in [loc.get("city"), loc.get("country", "").upper()] if x),
                                parse_iso(j.get("releasedDate"))))
    return jobs


SOURCES = {
    "workday": fetch_workday,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "smartrecruiters": fetch_smartrecruiters,
    "iimjobs": fetch_iimjobs,
    "hirist": fetch_hirist,
    "linkedin": fetch_jobspy("linkedin"),
    "naukri": fetch_jobspy("naukri"),
    "indeed": fetch_jobspy("indeed"),
    "glassdoor": fetch_jobspy("glassdoor"),
}


# ---------- filtering ----------

def term_pattern(term):
    return re.compile(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(term.lower()))


def fingerprint(j):
    """Same company + title = same job, whichever board it came from (LinkedIn, Naukri, Indeed... overlap a lot)."""
    def norm(s):
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    company = re.sub(r"\b(private|pvt|limited|ltd|inc|llp|technologies|india)\b", "", norm(j["company"]))
    return "dup:%s|%s" % (" ".join(company.split()), norm(j["title"]))


def filter_jobs(jobs, config):
    include = [term_pattern(t) for t in config["title_include"]]
    exclude = [term_pattern(t) for t in config["title_exclude"]]
    priority = [l.lower() for l in config["locations"]["priority"]]
    also_ok = [l.lower() for l in config["locations"]["also_ok"]]
    kept = {}
    for j in jobs:
        title = j["title"].lower()
        if not any(p.search(title) for p in include) or any(p.search(title) for p in exclude):
            continue
        loc = j["location"].lower()
        if any(l in loc for l in priority):
            j["tier"] = 0  # Pune
        elif any(l in loc for l in also_ok):
            j["tier"] = 1
        else:
            continue
        j["fp"] = fingerprint(j)
        kept.setdefault(j["fp"], j)  # dedupe: same job from several queries or several boards
    return list(kept.values())


# ---------- telegram ----------

def telegram(method, payload):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/%s" % (token, method),
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:  # rate limited: wait as long as Telegram asks
                try:
                    wait = json.loads(e.read()).get("parameters", {}).get("retry_after", 5)
                except ValueError:
                    wait = 5
                log("telegram rate limit, waiting %ss" % wait)
                time.sleep(min(wait, 60) + 1)
            elif e.code >= 500 and attempt < 3:
                time.sleep(3 * (attempt + 1))
            else:
                raise
        except (urllib.error.URLError, OSError) as e:  # timeouts, connection resets
            if attempt == 3:
                raise
            log("telegram %s failed (%s), retrying" % (method, e))
            time.sleep(3 * (attempt + 1))


def send_telegram(text, admin=False):
    """admin=True sends to TELEGRAM_ADMIN_CHAT_ID (whoever maintains the watcher), if set."""
    chat_id = (admin and os.environ.get("TELEGRAM_ADMIN_CHAT_ID")) or os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        raise SystemExit("TELEGRAM_CHAT_ID is not set (run: python3 job_watcher.py --get-chat-id)")
    try:
        telegram("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                 "disable_web_page_preview": True})
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        # Telegram rejected the formatting: resend as plain text so the message still arrives
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))
        telegram("sendMessage", {"chat_id": chat_id, "text": plain, "disable_web_page_preview": True})


def link_messages(links, limit=4000):
    """All apply links as bare URLs, one per line, split to fit Telegram's 4096-character limit."""
    chunks, current = [], ""
    for url in links:
        if current and len(current) + 1 + len(url) > limit:
            chunks.append(current)
            current = ""
        current = current + "\n" + url if current else url
    if current:
        chunks.append(current)
    return chunks


def send_links(text):
    # Plain text (no parse_mode) so the full URLs show as-is, ready to copy and forward
    telegram("sendMessage", {"chat_id": os.environ.get("TELEGRAM_CHAT_ID"), "text": text,
                             "disable_web_page_preview": True})


def format_job(j):
    def e(s):
        return html.escape(s, quote=False)
    badge = "📍 Pune" if j["tier"] == 0 else "📍 " + j["location"]
    lines = ["💼 <b>%s</b>" % e(j["title"]), "🏢 %s" % e(j["company"] or "?"), e(badge)]
    if j.get("posted"):
        lines.append("🕒 Posted %s" % j["posted"].strftime("%d %b"))
    lines.append('\n<a href="%s">View &amp; apply →</a>' % html.escape(j["url"] or "", quote=True))
    return "\n".join(lines)


# ---------- health ----------

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def update_health(health, results, sent_count, now):
    """results: {source: error string or None}. Returns Telegram messages to send."""
    messages = []
    for name, error in results.items():
        h = health["sources"].setdefault(name, {"fail_streak": 0, "alerted": False})
        if error is None:
            if h["alerted"]:
                messages.append("✅ Rakshita's job watcher: <b>%s</b> is working again." % name)
            h.update(fail_streak=0, alerted=False, last_ok=now.isoformat(), last_error=None)
            continue
        h["fail_streak"] += 1
        h["last_error"] = error[:300]
        if h["fail_streak"] >= FAIL_ALERT_AFTER and not h["alerted"]:
            h["alerted"] = True
            messages.append("⚠️ Rakshita's job watcher: <b>%s</b> has failed %d checks in a row.\nError: <code>%s</code>"
                            % (name, h["fail_streak"], html.escape(error[:200], quote=False)))

    day = health.setdefault("day", {})
    today = now.strftime("%Y-%m-%d")
    if day.get("date") != today:
        if day.get("date"):
            broken = [n for n, h in health["sources"].items() if h["fail_streak"]]
            messages.append("📊 <b>Rakshita's job watcher, daily check-in (%s)</b>\n%d checks ran, %d new jobs sent.\nSources: %s"
                            % (day["date"], day.get("runs", 0), day.get("sent", 0),
                               "all working ✅" if not broken else "problems with " + ", ".join(broken) + " ⚠️"))
        day.clear()
        day.update(date=today, runs=0, sent=0)
    day["runs"] += 1
    day["sent"] += sent_count
    return messages


# ---------- main ----------

def run(dry_run):
    config = load_json(CONFIG_PATH, None)
    seen = load_json(SEEN_PATH, {})
    health = load_json(HEALTH_PATH, {"sources": {}, "day": {}})
    now = datetime.now(timezone.utc)
    max_age = timedelta(days=config.get("max_age_days", 10))

    all_jobs, results = [], {}
    enabled = [(n, c) for n, c in config["sources"].items() if c.get("enabled")]
    with concurrent.futures.ThreadPoolExecutor(len(enabled)) as pool:
        futures = {pool.submit(SOURCES[n], c, config, now): n for n, c in enabled}
        for f in concurrent.futures.as_completed(futures):
            name = futures[f]
            try:
                found = f.result()
                log("%-16s %d postings" % (name, len(found)))
                all_jobs.extend(found)
                results[name] = None if found else "returned no postings (possibly blocked or changed)"
            except Exception as e:
                log("%-16s FAILED: %s" % (name, e))
                results[name] = "%s: %s" % (type(e).__name__, e)

    for key, error in PARTIAL_ERRORS.items():
        results[key] = error
    for key in health["sources"]:  # companies that failed before and worked this time
        if "/" in key and key not in results and results.get(key.split("/")[0]) is None:
            results[key] = None

    matched = filter_jobs(all_jobs, config)
    fresh = [j for j in matched if j["key"] not in seen and j["fp"] not in seen
             and (j["posted"] is None or now - j["posted"] <= max_age)]
    fresh.sort(key=lambda j: (j["tier"], -(j["posted"] or now - max_age).timestamp()))
    log("%d matching roles, %d new" % (len(matched), len(fresh)))

    to_send = fresh[:config.get("max_alerts_per_run", 20)]
    send_errors, failed_keys = [], set()
    for j in to_send:
        if dry_run:
            print(format_job(j) + "\n" + "-" * 40)
            continue
        try:
            send_telegram(format_job(j))
        except Exception as e:  # one bad message shouldn't stop the rest
            send_errors.append("%s: %s" % (j["title"][:60], e))
            failed_keys.add(j["key"])
            log("failed to send %r: %s" % (j["title"], e))
        time.sleep(1)
    if len(fresh) > len(to_send):
        note = "…and %d more matching jobs. They'll come in the next check." % (len(fresh) - len(to_send))
        print(note) if dry_run else send_telegram(note)
    links = [j["url"] for j in to_send if j["key"] not in failed_keys and j["url"]]
    for chunk in link_messages(links):
        try:
            print(chunk) if dry_run else send_links(chunk)
        except Exception as e:  # the jobs already went out: don't lose seen.json over this
            send_errors.append("apply links message: %s" % e)
            log("failed to send apply links: %s" % e)

    problems = update_health(health, results, len(to_send) - len(send_errors), now)
    if send_errors:
        problems.append("⚠️ Rakshita's job watcher: %d job alerts failed to send (will retry next check).\n<code>%s</code>"
                        % (len(send_errors), html.escape("\n".join(send_errors)[:500], quote=False)))
    for msg in problems:
        print(msg) if dry_run else send_telegram(msg, admin=True)

    if not dry_run:
        for j in to_send:  # unsent jobs (over the limit or failed to send) stay unseen, so they're retried next check
            if j["key"] not in failed_keys:
                seen[j["key"]] = seen[j["fp"]] = now.isoformat()
        cutoff = (now - timedelta(days=SEEN_RETENTION_DAYS)).isoformat()
        with open(SEEN_PATH, "w") as f:
            json.dump({k: v for k, v in seen.items() if v >= cutoff}, f, indent=0, sort_keys=True)
        with open(HEALTH_PATH, "w") as f:
            json.dump(health, f, indent=1, sort_keys=True)
    log("sent %d job alerts%s" % (len(to_send) - len(send_errors), " (dry run)" if dry_run else ""))


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-telegram", action="store_true")
    parser.add_argument("--get-chat-id", action="store_true")
    args = parser.parse_args()

    if args.get_chat_id:
        updates = telegram("getUpdates", {}).get("result", [])
        chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name") for u in updates if "message" in u}
        if not chats:
            print("No messages yet. Open the bot in Telegram, send it any message, then run this again.")
        for chat_id, name in chats.items():
            print("TELEGRAM_CHAT_ID=%s   (%s)" % (chat_id, name))
    elif args.test_telegram:
        send_telegram("✅ Job alerts are connected. New matching jobs will show up here.")
        print("Test message sent.")
    else:
        run(args.dry_run)


if __name__ == "__main__":
    main()
