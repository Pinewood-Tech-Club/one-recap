"""
Recap MVP server.

Flow:
- GET /            : Landing page with CTA to connect Schoology
- GET /auth/start  : Begin three-legged OAuth
- GET /auth/callback : Complete OAuth, queue recap job, redirect to /recap?id={uuid}
- GET /recap       : Frontend shell that polls /api/job/{id} for recap data
- GET /api/job/{id}: Job status + slides JSON
"""

import os
import uuid
import logging
import sys
import json
import sqlite3
import threading
import time
import queue
from datetime import datetime, timedelta
from collections import defaultdict
from types import SimpleNamespace

from flask import (
    Flask,
    render_template,
    redirect,
    request,
    url_for,
    jsonify,
    session,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_sock import Sock

# Optional dotenv load for local dev
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import schoolopy
import requests_oauthlib

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # trust reverse proxy for scheme/host
sock = Sock(app)

# Config
SCHOOLOGY_CONSUMER_KEY = os.environ.get("SCHOOLOGY_CONSUMER_KEY")
SCHOOLOGY_CONSUMER_SECRET = os.environ.get("SCHOOLOGY_CONSUMER_SECRET")
SCHOOLOGY_DOMAIN = os.environ.get("SCHOOLOGY_DOMAIN", "https://app.schoology.com")
SCHOOLOGY_API_DOMAIN = os.environ.get("SCHOOLOGY_API_DOMAIN", "https://api.schoology.com")
JOB_DB_PATH = os.environ.get("JOB_DB_PATH", "/data/jobs.db")
TWO_LEGGED_DEBUG = os.environ.get("TWO_LEGGED_DEBUG", "").lower() == "true"
DEBUG_EMAIL = os.environ.get("DEBUG_EMAIL", "debug@example.com")
VERBOSE_PROGRESS = os.environ.get("VERBOSE_PROGRESS", "").lower() == "true"

# WebSocket subscriber registry: job_id -> list[queue.Queue]
subscribers: dict[str, list[queue.Queue]] = {}

if not SCHOOLOGY_CONSUMER_KEY or not SCHOOLOGY_CONSUMER_SECRET:
    logger.warning("Schoology consumer key/secret missing; OAuth will fail.")

# Initialize jobs database ---------------------------------------------------
def init_job_db():
    conn = sqlite3.connect(JOB_DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            status TEXT,
            created_at TEXT,
            email TEXT,
            access_token TEXT,
            access_token_secret TEXT,
            two_legged INTEGER DEFAULT 0,
            slides_json TEXT,
            progress_json TEXT,
            error TEXT
        )
        """
    )
    # Add two_legged column if missing (migration-friendly)
    cur.execute("PRAGMA table_info(jobs)")
    cols = [row[1] for row in cur.fetchall()]
    if "two_legged" not in cols:
        cur.execute("ALTER TABLE jobs ADD COLUMN two_legged INTEGER DEFAULT 0")
    if "progress_json" not in cols:
        cur.execute("ALTER TABLE jobs ADD COLUMN progress_json TEXT")
    conn.commit()
    conn.close()


init_job_db()


# Background worker ----------------------------------------------------------
# Job persistence helpers ---------------------------------------------------
def get_conn():
    return sqlite3.connect(JOB_DB_PATH, check_same_thread=False)


def create_job(job_id, email, access_token, access_token_secret, two_legged: bool = False):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO jobs (id, status, created_at, email, access_token, access_token_secret, two_legged, slides_json, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            "queued",
            datetime.utcnow().isoformat(),
            email,
            access_token,
            access_token_secret,
            1 if two_legged else 0,
            None,
            None,
        ),
    )
    conn.commit()
    conn.close()


def update_job_status(job_id, status, error=None, progress: dict | None = None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE jobs SET status = ?, error = ?, progress_json = ? WHERE id = ?",
        (status, error, json.dumps(progress) if progress else None, job_id),
    )
    conn.commit()
    conn.close()


def save_job_result(job_id, slides):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE jobs SET slides_json = ?, status = ?, progress_json = ? WHERE id = ?",
        (json.dumps(slides), "done", None, job_id),
    )
    conn.commit()
    conn.close()

    notify_progress(job_id, {"status": "done", "slides": slides})


def fetch_job(job_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, status, created_at, email, slides_json, error, progress_json FROM jobs WHERE id = ?",
        (job_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    slides = json.loads(row[4]) if row[4] else None
    progress = json.loads(row[6]) if row[6] else None
    return {
        "id": row[0],
        "status": row[1],
        "created_at": row[2],
        "email": row[3],
        "slides": slides,
        "error": row[5],
        "progress": progress,
    }


def notify_progress(job_id: str, payload: dict):
    """Notify WebSocket subscribers and persist progress snapshot."""
    # push to subscribers
    subs = subscribers.get(job_id, [])
    for q in subs:
        q.put(payload)
    # persist snapshot
    update_job_status(job_id, status=payload.get("status", "running"), progress=payload)


def claim_next_job():
    """Atomically claim the next queued job."""
    conn = get_conn()
    conn.isolation_level = "EXCLUSIVE"
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return None
    job_id = row[0]
    cur.execute(
        "UPDATE jobs SET status = 'running' WHERE id = ? AND status = 'queued'",
        (job_id,),
    )
    if cur.rowcount == 1:
        cur.execute(
            "SELECT id, email, access_token, access_token_secret, two_legged FROM jobs WHERE id = ?",
            (job_id,),
        )
        job_row = cur.fetchone()
        conn.commit()
        conn.close()
        return {
            "id": job_row[0],
            "email": job_row[1],
            "access_token": job_row[2],
            "access_token_secret": job_row[3],
            "two_legged": bool(job_row[4]),
        }
    conn.commit()
    conn.close()
    return None


# Background worker ----------------------------------------------------------
def worker():
    while True:
        job = claim_next_job()
        if not job:
            time.sleep(2)
            continue
        job_id = job["id"]
        logger.info("Processing job %s", job_id)
        try:
            notify_progress(job_id, {"status": "running", "message": "Starting job"})
            slides = build_recap(
                {
                    "job_id": job_id,
                    "access_token": job["access_token"],
                    "access_token_secret": job["access_token_secret"],
                    "email": job["email"],
                    "two_legged": job.get("two_legged", False),
                }
            )
            save_job_result(job_id, slides)
            # TODO: send SES email here with link to /recap?id={job_id}
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Job %s failed", job_id)
            update_job_status(job_id, "error", error=str(exc))
            notify_progress(job_id, {"status": "error", "error": str(exc)})


worker_thread = threading.Thread(target=worker, daemon=True)
worker_thread.start()


# Helpers -------------------------------------------------------------------
def create_schoology_client(access_token: str | None, access_token_secret: str | None, two_legged: bool = False):
    """Create a Schoology client. Supports two-legged debug mode when flagged."""
    auth = schoolopy.Auth(
        SCHOOLOGY_CONSUMER_KEY,
        SCHOOLOGY_CONSUMER_SECRET,
        three_legged=not two_legged,
        domain=SCHOOLOGY_DOMAIN,
        access_token=access_token,
        access_token_secret=access_token_secret,
    )
    if two_legged:
        auth.oauth = requests_oauthlib.OAuth1Session(
            SCHOOLOGY_CONSUMER_KEY,
            client_secret=SCHOOLOGY_CONSUMER_SECRET,
        )
    else:
        # schoolopy Auth __init__ sets oauth with only consumer creds; rebuild with access tokens
        auth.oauth = requests_oauthlib.OAuth1Session(
            SCHOOLOGY_CONSUMER_KEY,
            client_secret=SCHOOLOGY_CONSUMER_SECRET,
            resource_owner_key=access_token,
            resource_owner_secret=access_token_secret,
        )
    sc = schoolopy.Schoology(auth)
    sc.limit = 200  # reduce pagination pressure where honored
    return sc, auth


def get_latest_user_submission(sc, auth, section_id: str, assignment_id: str, user_id: str):
    """
    Fetch latest submission for a user on an assignment.
    Uses submissions/revisions endpoint with all_revisions.
    """
    if not user_id:
        return None
    subs = []

    # Primary endpoint: list revisions for assignment, filter by uid
    try:
        url = f"{SCHOOLOGY_API_DOMAIN}/v1/sections/{section_id}/submissions/{assignment_id}/?all_revisions=true&with_attachments=true"
        resp = auth.oauth.get(url)
        if resp.status_code == 200:
            data = resp.json() or {}
            revs = data.get("revision") or []
            subs = [to_obj(r) for r in revs if str(r.get("uid", "")) == str(user_id)]
    except Exception:
        subs = []

    # Fallback: user-specific revision endpoint
    if not subs:
        try:
            url = f"{SCHOOLOGY_API_DOMAIN}/v1/sections/{section_id}/submissions/{assignment_id}/{user_id}?all_revisions=true&with_attachments=true"
            resp = auth.oauth.get(url)
            if resp.status_code == 200:
                data = resp.json() or {}
                revs = data.get("revision") or data.get("submission") or []
                if isinstance(revs, dict) and "revision" in revs:
                    revs = revs["revision"]
                subs = [to_obj(r) for r in revs] if isinstance(revs, list) else []
        except Exception:
            subs = []

    def sub_timestamp(sub_obj):
        ts = parse_dt(getattr(sub_obj, "submitted", None)) or parse_dt(getattr(sub_obj, "created", None))
        return ts or datetime.min

    latest = None
    if subs:
        latest = max(subs, key=sub_timestamp)
    return latest


def parse_dt(value):
    """Parse Schoology datetime (string or epoch) to naive datetime; return None on failure."""
    if not value:
        return None
    # epoch int/str
    if isinstance(value, (int, float)):
        try:
            return datetime.utcfromtimestamp(float(value))
        except Exception:
            pass
    if isinstance(value, str) and value.isdigit():
        try:
            return datetime.utcfromtimestamp(float(value))
        except Exception:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except Exception:
            continue
    return None


def to_obj(item: dict):
    """Lightweight object wrapper for dicts."""
    return SimpleNamespace(**item)


def paginated_list(auth, path: str, key: str | None = None):
    """
    Fetch all pages for a Schoology collection endpoint.
    Returns list of dicts.
    """
    items = []
    url = f"{SCHOOLOGY_API_DOMAIN}/v1/{path}"
    while url:
        resp = auth.oauth.get(url)
        resp.raise_for_status()
        data = resp.json() or {}
        target_key = key
        if not target_key:
            # best-effort: pick the first list-valued key that's not links
            for k, v in data.items():
                if isinstance(v, list):
                    target_key = k
                    break
        if target_key and isinstance(data.get(target_key), list):
            items.extend(data.get(target_key, []))
        links = data.get("links", {}) or {}
        url = links.get("next")
    return items


def build_recap(payload):
    """
    Fetch Schoology data and compute recap slides.
    This is intentionally light-weight and defensive for MVP.
    """
    job_id = payload["job_id"]
    access_token = payload["access_token"]
    access_token_secret = payload["access_token_secret"]
    user_email = payload.get("email")

    sc, auth = create_schoology_client(access_token, access_token_secret, two_legged=payload.get("two_legged", False))

    # Determine user_id robustly; allow debug override
    me = None
    try:
        me = sc.get_me()
    except Exception:
        me = None
    user_id = getattr(me, "uid", None) if me else None
    schoology_user = {
        "id": user_id,
        "name": getattr(me, "name_display", "") if me else "",
        "email": user_email or (getattr(me, "primary_email", "") if me else ""),
    }
    notify_progress(job_id, {"status": "running", "stage": "me", "user_id": user_id})

    # Data buckets
    sections_raw = []
    if user_id:
        try:
            sections_raw = paginated_list(auth, f"users/{user_id}/sections", key="section")
        except Exception:
            sections_raw = []
    sections = [to_obj(s) for s in sections_raw]
    if not sections:
        try:
            sections = sc.get_sections() or []
        except Exception:
            sections = []

    notify_progress(job_id, {"status": "running", "stage": "sections", "count": len(sections)})

    # Enrollment cache for classmate counts (paged)
    section_enrollments = {}
    for section in sections:
        try:
            enrollments_raw = paginated_list(auth, f"sections/{section.id}/enrollments", key="enrollment")
            section_enrollments[section.id] = [to_obj(e) for e in enrollments_raw]
        except Exception:
            section_enrollments[section.id] = []

    assignments_by_section = defaultdict(list)
    grades_lookup = {}  # assignment_id -> grade (float)
    # Store only the latest submission per assignment for this user
    latest_submissions: dict[str, SimpleNamespace] = {}
    section_lookup = {}
    processed_assignments = 0

    for section in sections:
        section_lookup[section.id] = section
        try:
            assignments_raw = paginated_list(auth, f"sections/{section.id}/assignments", key="assignment")
            assignments = [to_obj(a) for a in assignments_raw]
            assignments_by_section[section.id] = assignments

            for assignment in assignments:
                subs_raw = []
                try:
                    subs_raw = paginated_list(
                        auth,
                        f"sections/{section.id}/assignments/{assignment.id}/submissions",
                        key="submission",
                    )
                except Exception:
                    subs_raw = []
                # Filter to the current user and keep the latest submission
                latest = get_latest_user_submission(sc, auth, section.id, assignment.id, user_id)
                if latest:
                    latest._section_id = section.id  # noqa: SLF001
                    latest._assignment_id = assignment.id  # noqa: SLF001
                    latest_submissions[str(assignment.id)] = latest
                if VERBOSE_PROGRESS:
                    logger.info(
                        "Assignment processed %s / section %s / subs_seen=%s / latest_for_user=%s",
                        getattr(assignment, "title", ""),
                        getattr(section, "course_title", ""),
                        len(subs_raw or []),
                        str(str(assignment.id) in latest_submissions),
                    )
                processed_assignments += 1
                if processed_assignments % 10 == 0:
                    notify_progress(
                        job_id,
                        {
                            "status": "running",
                            "stage": "assignments",
                            "section": getattr(section, "course_title", ""),
                            "processed": processed_assignments,
                        },
                    )
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("Failed assignments for section %s: %s", getattr(section, "id", "?"), e)
            continue

    now = datetime.utcnow()

    assignment_lookup = {}
    for assigns in assignments_by_section.values():
        for a in assigns:
            assignment_lookup[str(a.id)] = a

    # Metrics ---------------------------------------------------------------
    # Missing assignments (debug_missing_late logic): allow_dropbox == 1 AND no submission for this user
    missing = 0
    missing_per_course = defaultdict(int)
    submitted_assignment_ids = set(latest_submissions.keys())
    missing_ids = set()
    for section in sections:
        assigns = assignments_by_section.get(section.id, [])
        for a in assigns:
            aid = str(getattr(a, "id", ""))
            allow_dropbox = str(getattr(a, "allow_dropbox", "1")) == "1"
            if allow_dropbox and aid not in submitted_assignment_ids:
                missing += 1
                missing_per_course[section.id] += 1
                missing_ids.add(aid)

    # Busiest month
    month_counts = defaultdict(int)
    for assigns in assignments_by_section.values():
        for a in assigns:
            due = parse_dt(getattr(a, "due", None))
            if due:
                month_counts[due.strftime("%B")] += 1

    busiest_month = None
    if month_counts:
        busiest_month = max(month_counts.items(), key=lambda x: x[1])

    # Course with most assignments
    course_assignment_counts = {}
    for section in sections:
        course_assignment_counts[section.id] = len(assignments_by_section.get(section.id, []))
    top_assignment_course = None
    if course_assignment_counts:
        top_assignment_course = max(course_assignment_counts.items(), key=lambda x: x[1])

    # Class size champion
    class_sizes = {}
    for section in sections:
        class_sizes[section.id] = len(section_enrollments.get(section.id, []))
    class_size_champ = None
    if class_sizes:
        class_size_champ = max(class_sizes.items(), key=lambda x: x[1])

    # Weekend / Weekday / Night owl
    weekend_subs = weekday_subs = night_owl_subs = 0
    for sub in latest_submissions.values():
        submitted = parse_dt(getattr(sub, "submitted", None)) or parse_dt(getattr(sub, "created", None))
        if not submitted:
            continue
        if submitted.weekday() >= 5:
            weekend_subs += 1
        else:
            weekday_subs += 1
        if submitted.hour >= 22:
            night_owl_subs += 1

    total_subs = weekend_subs + weekday_subs or 1
    night_pct = round((night_owl_subs / total_subs) * 100, 1)

    # Procrastination metrics (debug script aligned)
    deltas = []
    early_birds = 0
    late_submissions = 0
    on_time_flags = []
    for sub in latest_submissions.values():
        assignment = assignment_lookup.get(str(getattr(sub, "_assignment_id", "")))
        due = parse_dt(getattr(assignment, "due", None)) if assignment else None
        submitted = parse_dt(getattr(sub, "submitted", None)) or parse_dt(getattr(sub, "created", None))
        if not submitted:
            continue
        aid = str(getattr(assignment, "id", "")) if assignment else ""
        if aid in missing_ids:
            continue
        is_late_flag = bool(getattr(sub, "late", False))
        is_late = (submitted and due and submitted > due) or is_late_flag
        is_on_time = not is_late
        on_time_flags.append(is_on_time)

        if is_on_time and due:
            delta = due - submitted
            deltas.append(delta)
            if delta >= timedelta(hours=48):
                early_birds += 1
        if is_late:
            late_submissions += 1

    avg_procrastination = None
    if deltas:
        avg_procrastination = sum(deltas, timedelta()) / len(deltas)

    # Missing assignments (debug script aligned): allow_dropbox==1 and no submission for this user
    missing = 0
    missing_per_course = defaultdict(int)
    missing_ids = set()
    for section in sections:
        assigns = assignments_by_section.get(section.id, [])
        for a in assigns:
            aid = str(getattr(a, "id", ""))
            allow_dropbox = str(getattr(a, "allow_dropbox", "1")) == "1"
            latest = get_latest_user_submission(sc, auth, section.id, aid, user_id)
            if allow_dropbox and not latest:
                missing += 1
                missing_per_course[section.id] += 1
                missing_ids.add(aid)

    most_missing_course = None
    if missing_per_course:
        most_missing_course = max(missing_per_course.items(), key=lambda x: x[1])

    # Consistency streak (approximate)
    streak = 0
    best_streak = 0
    for flag in sorted(
        on_time_flags, reverse=False
    ):
        if flag:
            streak += 1
            best_streak = max(best_streak, streak)
        else:
            streak = 0

    # Workload peaks
    week_counts = defaultdict(int)
    for assigns in assignments_by_section.values():
        for a in assigns:
            due = parse_dt(getattr(a, "due", None))
            if not due:
                continue
            week_start = (due - timedelta(days=due.weekday())).date()
            week_counts[week_start] += 1

    peak_week = drought_week = None
    if week_counts:
        peak_week = max(week_counts.items(), key=lambda x: x[1])
        drought_week = min(week_counts.items(), key=lambda x: x[1])

    # Attachment stats (latest submissions only)
    total_files = 0
    max_file_size = 0
    for sub in latest_submissions.values():
        attachments = getattr(sub, "attachments", None)
        if attachments and hasattr(attachments, "files") and getattr(attachments.files, "file", None):
            files = attachments.files.file
            if isinstance(files, list):
                total_files += len(files)
                for f in files:
                    size = getattr(f, "filesize", 0) or 0
                    try:
                        size = int(size)
                    except Exception:
                        size = 0
                    max_file_size = max(max_file_size, size)
            else:
                total_files += 1
                size = getattr(files, "filesize", 0) or 0
                try:
                    size = int(size)
                except Exception:
                    size = 0
                max_file_size = max(max_file_size, size)

    # Classroom constants (top classmates by shared sections)
    classmate_counts = defaultdict(lambda: {"count": 0, "sections": set(), "name": ""})
    for section in sections:
        enrolls = section_enrollments.get(section.id, [])
        for enr in enrolls:
            if str(getattr(enr, "uid", "")) == str(user_id):
                continue
            classmate_counts[enr.uid]["count"] += 1
            classmate_counts[enr.uid]["sections"].add(getattr(section, "course_title", ""))
            classmate_counts[enr.uid]["name"] = getattr(enr, "name_display", f"User {enr.uid}")

    top_classmates = sorted(classmate_counts.items(), key=lambda x: x[1]["count"], reverse=True)[:5]

    # Build slides ----------------------------------------------------------
    slides = []

    def add_slide(title, big, bottom, extra=None):
        slides.append({"title": title, "big": big, "bottom": bottom, "extra": extra or {}})

    # Slide order (12 requested)
    add_slide(
        "Busiest Month",
        busiest_month[0] if busiest_month else "—",
        f"You had {busiest_month[1]} assignments due in {busiest_month[0]}!"
        if busiest_month
        else "No assignments found.",
    )

    add_slide(
        "Course with Most Assignments",
        getattr(section_lookup.get(top_assignment_course[0]), "course_title", "—") if top_assignment_course else "—",
        f"{top_assignment_course[1]} assignments this year" if top_assignment_course else "No data.",
    )

    add_slide(
        "Class Size Champion",
        getattr(section_lookup.get(class_size_champ[0]), "course_title", "—") if class_size_champ else "—",
        f"You shared this class with {class_size_champ[1]} classmates" if class_size_champ else "No enrollments found.",
    )

    add_slide("Weekend Warrior", weekend_subs, "assignments submitted on weekends")
    add_slide("Weekday Grinder", weekday_subs, "assignments submitted on weekdays")
    add_slide(
        "Night Owl Score",
        night_owl_subs,
        f"assignments submitted after 10pm... that's {night_pct}% of assignments!",
    )

    # Average procrastination
    def format_delta(td: timedelta):
        total_hours = td.total_seconds() / 3600
        if total_hours >= 48:
            return f"{total_hours/24:.1f} days"
        if total_hours >= 0:
            return f"{total_hours:.0f} hours"
        if total_hours < 0:
            return f"{total_hours:.0f} hours"

    if avg_procrastination is not None:
        delta_text = format_delta(avg_procrastination)
        # Custom bottom text based on cutoff
        hours = avg_procrastination.total_seconds() / 3600
        if hours < 1:
            bottom_text = f"{delta_text}... wow, you're really cutting it close!"
        elif hours > 48:
            bottom_text = f"{delta_text}... wow, you're really organized!"
        else:
            bottom_text = f"{delta_text} before the deadline (pretty good!)"
        add_slide("Average Procrastination", delta_text, bottom_text)
    else:
        add_slide("Average Procrastination", "—", "No submissions with due dates.")

    total_submissions = len(latest_submissions) or 1
    early_pct = round((early_birds / total_submissions) * 100, 1)
    add_slide(
        "Early Bird",
        early_birds,
        f"assignments submitted more than 48 hours early... that's {early_pct}% of assignments!",
    )

    total_submissions = len(latest_submissions) or 1
    late_pct = round((late_submissions / total_submissions) * 100, 1)
    add_slide(
        "Late Ledger",
        late_submissions,
        f"late submissions... that's {late_pct}% of assignments!",
    )

    add_slide(
        "Missing Watch",
        missing,
        "missing assignments (and you didn't turn these ones in...)" if missing else "No missing assignments!",
    )

    # Most missing course with percent within that course
    if most_missing_course:
        course_obj = section_lookup.get(most_missing_course[0])
        total_in_course = len(assignments_by_section.get(most_missing_course[0], [])) or 1
        pct = round((most_missing_course[1] / total_in_course) * 100, 1)
        add_slide(
            "Most Missing Course",
            getattr(course_obj, "course_title", "—"),
            f"{most_missing_course[1]} missing assignments... that's {pct}% of assignments!",
        )
    else:
        add_slide("Most Missing Course", "—", "No missing assignments!")

    # Classroom constants list
    classmates_list = []
    for uid, info in top_classmates:
        classmates_list.append(
            {
                "name": info["name"],
                "count": info["count"],
                "sections": sorted(list(info["sections"]))[:5],
            }
        )
    slides.append(
        {
            "title": "Your Classroom Constants",
            "big": "",
            "bottom": "You shared a lot of classes with these classmates!",
            "list": classmates_list,
        }
    )

    # Summary bento (shareable 8 cards)
    grid_cards = []
    grid_cards.append(
        {
            "label": "Busiest Month",
            "value": busiest_month[0] if busiest_month else "—",
        }
    )
    grid_cards.append(
        {
            "label": "Most Assignments",
            "value": getattr(section_lookup.get(top_assignment_course[0]), "course_title", "—")
            if top_assignment_course
            else "—",
        }
    )
    grid_cards.append(
        {
            "label": "Class Size",
            "value": getattr(section_lookup.get(class_size_champ[0]), "course_title", "—")
            if class_size_champ
            else "—",
        }
    )
    grid_cards.append({"label": "Weekend Warrior", "value": weekend_subs})
    grid_cards.append({"label": "Night Owl", "value": f"{night_owl_subs} ({night_pct}%)"})
    grid_cards.append(
        {
            "label": "Avg Procrastination",
            "value": format_delta(avg_procrastination) if avg_procrastination else "—",
        }
    )
    grid_cards.append({"label": "Late Ledger", "value": late_submissions})
    grid_cards.append({"label": "Missing Watch", "value": missing})

    slides.append(
        {
            "title": "Recap Highlights",
            "big": "",
            "bottom": "",
            "grid": grid_cards,
            "layout": "grid",
        }
    )

    return slides


# Routes --------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/auth/start")
def auth_start():
    """Kick off Schoology OAuth."""
    if TWO_LEGGED_DEBUG:
        job_id = str(uuid.uuid4())
        create_job(job_id, DEBUG_EMAIL, None, None, two_legged=True)
        return redirect(url_for("recap_view", id=job_id))

    if not SCHOOLOGY_CONSUMER_KEY or not SCHOOLOGY_CONSUMER_SECRET:
        return "Missing Schoology API keys. Set SCHOOLOGY_CONSUMER_KEY/SECRET.", 500

    callback_url = url_for("auth_callback", _external=True)
    auth = schoolopy.Auth(
        SCHOOLOGY_CONSUMER_KEY,
        SCHOOLOGY_CONSUMER_SECRET,
        three_legged=True,
        domain=SCHOOLOGY_DOMAIN,
    )
    url = auth.request_authorization(callback_url=callback_url)
    if auth.request_token and auth.request_token_secret:
        session["request_token"] = auth.request_token
        session["request_token_secret"] = auth.request_token_secret
    return redirect(url)


@app.route("/auth/callback")
def auth_callback():
    oauth_token = request.args.get("oauth_token")
    if not oauth_token:
        return redirect(url_for("index", error="missing_oauth_token"))

    req_token = session.pop("request_token", None)
    req_secret = session.pop("request_token_secret", None)
    if not req_token or oauth_token != req_token or not req_secret:
        return redirect(url_for("index", error="missing_request_secret"))

    auth = schoolopy.Auth(
        SCHOOLOGY_CONSUMER_KEY,
        SCHOOLOGY_CONSUMER_SECRET,
        three_legged=True,
        domain=SCHOOLOGY_DOMAIN,
        request_token=oauth_token,
        request_token_secret=req_secret,
    )

    if not auth.authorize():
        return redirect(url_for("index", error="authorize_failed"))

    access_token = auth.access_token
    access_token_secret = auth.access_token_secret

    # Rebuild oauth session with access tokens (schoolopy does not do this automatically)
    auth.oauth = requests_oauthlib.OAuth1Session(
        SCHOOLOGY_CONSUMER_KEY,
        client_secret=SCHOOLOGY_CONSUMER_SECRET,
        resource_owner_key=access_token,
        resource_owner_secret=access_token_secret,
    )

    # Fetch user to capture email
    sc = schoolopy.Schoology(auth)
    me = sc.get_me()
    email = getattr(me, "primary_email", None)

    job_id = str(uuid.uuid4())
    create_job(job_id, email, access_token, access_token_secret)

    return redirect(url_for("recap_view", id=job_id))


@app.route("/recap")
def recap_view():
    job_id = request.args.get("id")
    return render_template("recap.html", job_id=job_id)


@app.route("/api/job/<job_id>")
def job_status(job_id):
    job = fetch_job(job_id)
    if not job:
        return jsonify({"error": "not_found"}), 404
    return jsonify(job)


# WebSocket for live progress updates
@sock.route("/ws/job/<job_id>")
def job_ws(ws, job_id):
    # send initial state
    job = fetch_job(job_id)
    if job:
        ws.send(json.dumps(job))
    # subscribe
    q = queue.Queue()
    subscribers.setdefault(job_id, []).append(q)
    try:
        while True:
            payload = q.get()
            ws.send(json.dumps(payload))
    except Exception:
        pass
    finally:
        # cleanup
        subs = subscribers.get(job_id, [])
        if q in subs:
            subs.remove(q)


if __name__ == "__main__":
    app.run(debug=True, port=5002)
