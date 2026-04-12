"""
TradeMe Scraper — Flask UI
Run with: python app.py
Then open http://localhost:5000
"""

import atexit
import json
import sqlite3
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "trademe_cars.db"
SCRAPER_FILTERS_PATH = BASE_DIR / "scraper_filters.json"
MAILER_FILTERS_PATH  = BASE_DIR / "mailer_filters.json"
SCHEDULE_PATH        = BASE_DIR / "schedule_config.json"

jobs = {}  # job_id -> {"output": str, "done": bool, "ok": bool}
last_scheduled_run = {}  # "scraper"/"mailer" -> {"time", "ok", "output"}

scheduler = BackgroundScheduler(timezone="Pacific/Auckland")


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def load_json(path):
    if path.exists():
        return json.loads(path.read_text())
    return []


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2))


def load_schedule() -> dict:
    if SCHEDULE_PATH.exists():
        return json.loads(SCHEDULE_PATH.read_text())
    return {
        "scraper": {"enabled": False, "hour": 8, "minute": 0,  "filter_names": []},
        "mailer":  {"enabled": False, "hour": 8, "minute": 30, "filter_names": []},
    }


def apply_schedule(config: dict):
    """Remove existing scheduled jobs and re-add from config."""
    for type_ in ["scraper", "mailer"]:
        job_id = f"scheduled_{type_}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        cfg = config.get(type_, {})
        if cfg.get("enabled"):
            scheduler.add_job(
                run_scheduled,
                CronTrigger(hour=cfg["hour"], minute=cfg["minute"]),
                args=[type_, cfg.get("filter_names", [])],
                id=job_id,
                replace_existing=True,
            )


def run_subprocess(job_id: str, cmd: list):
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(BASE_DIR),
        )
        output = ""
        for line in proc.stdout:
            output += line
            jobs[job_id]["output"] = output
        proc.wait()
        jobs[job_id]["done"] = True
        jobs[job_id]["ok"] = proc.returncode == 0
    except Exception as e:
        jobs[job_id]["output"] += f"\nError: {e}"
        jobs[job_id]["done"] = True
        jobs[job_id]["ok"] = False


def run_scheduled(type_: str, filter_names: list):
    """Called by APScheduler — runs scraper or mailer and records the result."""
    cmd = [sys.executable, str(BASE_DIR / f"{type_}.py")]
    if filter_names:
        cmd += ["--filters", ",".join(filter_names)]
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"output": "", "done": False, "ok": False}
    run_subprocess(job_id, cmd)  # blocks in scheduler thread — that's fine
    last_scheduled_run[type_] = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "ok":   jobs[job_id]["ok"],
        "output": jobs[job_id]["output"][-600:],  # keep last 600 chars
    }


def next_run_str(type_: str) -> str | None:
    job = scheduler.get_job(f"scheduled_{type_}")
    if job and job.next_run_time:
        return job.next_run_time.strftime("%Y-%m-%d %H:%M")
    return None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    try:
        con = get_db()
        total = con.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        today = con.execute(
            "SELECT COUNT(*) FROM listings WHERE date_scraped >= date('now')"
        ).fetchone()[0]
        week = con.execute(
            "SELECT COUNT(*) FROM listings WHERE date_scraped >= date('now', '-7 days')"
        ).fetchone()[0]
        con.close()
    except Exception:
        total = today = week = 0

    scraper_config = load_json(SCRAPER_FILTERS_PATH) or {"max_pages": 10, "filter_sets": []}
    scraper_names  = [f.get("name") or f"Filter {i+1}" for i, f in enumerate(scraper_config.get("filter_sets", []))]
    mailer_filters = load_json(MAILER_FILTERS_PATH) or []
    mailer_names   = [f.get("name") or f"Filter {i+1}" for i, f in enumerate(mailer_filters)]
    schedule       = load_schedule()

    return render_template(
        "index.html",
        total=total, today=today, week=week,
        scraper_names=scraper_names,
        mailer_names=mailer_names,
        schedule=schedule,
        scraper_next=next_run_str("scraper"),
        mailer_next=next_run_str("mailer"),
        scraper_last=last_scheduled_run.get("scraper"),
        mailer_last=last_scheduled_run.get("mailer"),
    )


@app.route("/listings")
def listings():
    page = max(1, int(request.args.get("page", 1)))
    per_page = 50
    offset = (page - 1) * per_page
    try:
        con = get_db()
        total = con.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        rows = con.execute(
            """
            SELECT listing_id, title, price, kilometres, year, make, model,
                   url, date_scraped, listed_as, region_id
            FROM listings
            ORDER BY date_scraped DESC
            LIMIT ? OFFSET ?
            """,
            (per_page, offset),
        ).fetchall()
        con.close()
    except Exception:
        total, rows = 0, []
    pages = max(1, (total + per_page - 1) // per_page)
    return render_template("listings.html", rows=rows, page=page, pages=pages, total=total)


@app.route("/filters")
def filters():
    scraper_config = load_json(SCRAPER_FILTERS_PATH) or {"max_pages": 10, "filter_sets": []}
    return render_template(
        "filters.html",
        scraper_config=scraper_config,
        mailer_filters=load_json(MAILER_FILTERS_PATH),
    )


@app.route("/filters/scraper", methods=["POST"])
def save_scraper_filters():
    save_json(SCRAPER_FILTERS_PATH, request.get_json())
    return jsonify({"ok": True})


@app.route("/filters/mailer", methods=["POST"])
def save_mailer_filters():
    save_json(MAILER_FILTERS_PATH, request.get_json())
    return jsonify({"ok": True})


@app.route("/schedule", methods=["POST"])
def save_schedule():
    config = request.get_json()
    save_json(SCHEDULE_PATH, config)
    apply_schedule(config)
    return jsonify({
        "ok": True,
        "scraper_next": next_run_str("scraper"),
        "mailer_next":  next_run_str("mailer"),
    })


@app.route("/run/scraper", methods=["POST"])
def run_scraper():
    body = request.get_json() or {}
    selected = body.get("filter_names", [])
    cmd = [sys.executable, str(BASE_DIR / "scraper.py")]
    if selected:
        cmd += ["--filters", ",".join(selected)]
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"output": "Starting scraper...\n", "done": False, "ok": False}
    t = threading.Thread(target=run_subprocess, args=(job_id, cmd))
    t.daemon = True
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/run/mailer", methods=["POST"])
def run_mailer():
    body = request.get_json() or {}
    selected = body.get("filter_names", [])
    cmd = [sys.executable, str(BASE_DIR / "mailer.py")]
    if selected:
        cmd += ["--filters", ",".join(selected)]
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"output": "Starting mailer...\n", "done": False, "ok": False}
    t = threading.Thread(target=run_subprocess, args=(job_id, cmd))
    t.daemon = True
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/run/status/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


if __name__ == "__main__":
    from waitress import serve
    scheduler.start()
    apply_schedule(load_schedule())
    atexit.register(lambda: scheduler.shutdown(wait=False))
    print("TradeMe Scraper UI running at http://localhost:5000")
    serve(app, host="127.0.0.1", port=5000, threads=4)
