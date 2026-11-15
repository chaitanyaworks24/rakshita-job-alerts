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


def http_json(url, body=None, retries=2):
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, data=json.dumps(body).encode() if body is not None else None,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise last_err


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
    return {"key": "%s:%s:%s" % (source, company, job_id), "source": source, "company": company,
            "title": title.strip(), "url": url, "location": (location or "").strip(), "posted": posted}


# ---------- sources ----------

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
}


# ---------- filtering ----------

def term_pattern(term):
    return re.compile(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(term.lower()))


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
        kept[j["key"]] = j  # dedupe: Workday returns the same job for several queries
    return list(kept.values())


# ---------- telegram ----------

def telegram(method, payload):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/%s" % (token, method),
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


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


def format_job(j):
    def e(s):
        return html.escape(s, quote=False)
    badge = "📍 Pune" if j["tier"] == 0 else "📍 " + j["location"]
    lines = ["💼 <b>%s</b>" % e(j["title"]), "🏢 %s" % e(j["company"]), e(badge)]
    if j.get("posted"):
        lines.append("🕒 Posted %s" % j["posted"].strftime("%d %b"))
    lines.append('\n<a href="%s">View &amp; apply →</a>' % html.escape(j["url"], quote=True))
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
    for name, cfg in config["sources"].items():
        if not cfg.get("enabled"):
            continue
        try:
            found = SOURCES[name](cfg, config, now)
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
    fresh = [j for j in matched if j["key"] not in seen and (j["posted"] is None or now - j["posted"] <= max_age)]
    fresh.sort(key=lambda j: (j["tier"], -(j["posted"] or now - max_age).timestamp()))
    log("%d matching roles, %d new" % (len(matched), len(fresh)))

    to_send = fresh[:config.get("max_alerts_per_run", 20)]
    send_errors = []
    for j in to_send:
        if dry_run:
            print(format_job(j) + "\n" + "-" * 40)
            continue
        try:
            send_telegram(format_job(j))
        except Exception as e:  # one bad message shouldn't stop the rest
            send_errors.append("%s: %s" % (j["title"][:60], e))
        time.sleep(1)
    if len(fresh) > len(to_send):
        note = "…and %d more matching jobs. They'll come in the next check." % (len(fresh) - len(to_send))
        print(note) if dry_run else send_telegram(note)

    problems = update_health(health, results, len(to_send) - len(send_errors), now)
    if send_errors:
        problems.append("⚠️ Rakshita's job watcher: %d job alerts failed to send.\n<code>%s</code>"
                        % (len(send_errors), html.escape("\n".join(send_errors)[:500], quote=False)))
    for msg in problems:
        print(msg) if dry_run else send_telegram(msg, admin=True)

    if not dry_run:
        for j in to_send:  # jobs over the per-run limit stay unseen, so they're sent next check
            seen[j["key"]] = now.isoformat()
        cutoff = (now - timedelta(days=SEEN_RETENTION_DAYS)).isoformat()
        with open(SEEN_PATH, "w") as f:
            json.dump({k: v for k, v in seen.items() if v >= cutoff}, f, indent=0, sort_keys=True)
        with open(HEALTH_PATH, "w") as f:
            json.dump(health, f, indent=1, sort_keys=True)
    log("sent %d job alerts%s" % (len(to_send), " (dry run)" if dry_run else ""))


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
