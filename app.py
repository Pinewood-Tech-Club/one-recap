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

# Config
SCHOOLOGY_CONSUMER_KEY = os.environ.get("SCHOOLOGY_CONSUMER_KEY")
SCHOOLOGY_CONSUMER_SECRET = os.environ.get("SCHOOLOGY_CONSUMER_SECRET")
SCHOOLOGY_DOMAIN = os.environ.get("SCHOOLOGY_DOMAIN", "https://app.schoology.com")
SCHOOLOGY_API_DOMAIN = os.environ.get("SCHOOLOGY_API_DOMAIN", "https://api.schoology.com")
JOB_DB_PATH = os.environ.get("JOB_DB_PATH", "/data/jobs.db")

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
            slides_json TEXT,
            error TEXT
        )
        """
    )
    conn.commit()
    conn.close()


init_job_db()


# Background worker ----------------------------------------------------------
# Job persistence helpers ---------------------------------------------------
def get_conn():
    return sqlite3.connect(JOB_DB_PATH, check_same_thread=False)


def create_job(job_id, email, access_token, access_token_secret):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO jobs (id, status, created_at, email, access_token, access_token_secret, slides_json, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            "queued",
            datetime.utcnow().isoformat(),
            email,
            access_token,
            access_token_secret,
            None,
            None,
        ),
    )
    conn.commit()
    conn.close()


def update_job_status(job_id, status, error=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE jobs SET status = ?, error = ? WHERE id = ?",
        (status, error, job_id),
    )
    conn.commit()
    conn.close()


def save_job_result(job_id, slides):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE jobs SET slides_json = ?, status = ? WHERE id = ?",
        (json.dumps(slides), "done", job_id),
    )
    conn.commit()
    conn.close()


def fetch_job(job_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, status, created_at, email, slides_json, error FROM jobs WHERE id = ?",
        (job_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    slides = json.loads(row[4]) if row[4] else None
    return {
        "id": row[0],
        "status": row[1],
        "created_at": row[2],
        "email": row[3],
        "slides": slides,
        "error": row[5],
    }


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
            "SELECT id, email, access_token, access_token_secret FROM jobs WHERE id = ?",
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
            update_job_status(job_id, "running")
            slides = build_recap(
                {
                    "job_id": job_id,
                    "access_token": job["access_token"],
                    "access_token_secret": job["access_token_secret"],
                    "email": job["email"],
                }
            )
            save_job_result(job_id, slides)
            update_job_status(job_id, "done")
            # TODO: send SES email here with link to /recap?id={job_id}
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Job %s failed", job_id)
            update_job_status(job_id, "error", error=str(exc))


worker_thread = threading.Thread(target=worker, daemon=True)
worker_thread.start()


# Helpers -------------------------------------------------------------------
def create_schoology_client(access_token: str, access_token_secret: str):
    """Create an authenticated Schoology client from access tokens."""
    auth = schoolopy.Auth(
        SCHOOLOGY_CONSUMER_KEY,
        SCHOOLOGY_CONSUMER_SECRET,
        three_legged=True,
        domain=SCHOOLOGY_DOMAIN,
        access_token=access_token,
        access_token_secret=access_token_secret,
    )
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


def parse_dt(value):
    """Parse Schoology datetime string to naive datetime; return None on failure."""
    if not value:
        return None
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

    sc, auth = create_schoology_client(access_token, access_token_secret)

    me = sc.get_me()
    user_id = getattr(me, "uid", None)
    schoology_user = {
        "id": user_id,
        "name": getattr(me, "name_display", ""),
        "email": user_email or getattr(me, "primary_email", ""),
    }

    # Data buckets
    sections_raw = paginated_list(auth, f"users/{user_id}/sections", key="section")
    sections = [to_obj(s) for s in sections_raw]

    # Enrollment cache for classmate counts (paged)
    section_enrollments = {}
    for section in sections:
        try:
            enrollments_raw = paginated_list(auth, f"sections/{section.id}/enrollments", key="enrollment")
            section_enrollments[section.id] = [to_obj(e) for e in enrollments_raw]
        except Exception:
            section_enrollments[section.id] = []

    assignments_by_section = defaultdict(list)
    submissions = []
    section_lookup = {}

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
                for sub in subs_raw or []:
                    sub_obj = to_obj(sub)
                    sub_obj._section_id = section.id  # noqa: SLF001
                    sub_obj._assignment_id = assignment.id  # noqa: SLF001
                    submissions.append(sub_obj)
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("Failed assignments for section %s: %s", getattr(section, "id", "?"), e)
            continue

    now = datetime.utcnow()

    assignment_lookup = {}
    for assigns in assignments_by_section.values():
        for a in assigns:
            assignment_lookup[str(a.id)] = a

    # Metrics ---------------------------------------------------------------
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
    for sub in submissions:
        submitted = parse_dt(getattr(sub, "created", None)) or parse_dt(getattr(sub, "submitted", None))
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

    # Procrastination metrics
    deltas = []
    early_birds = 0
    late_submissions = 0
    on_time_flags = []
    for sub in submissions:
        assignment = assignment_lookup.get(str(getattr(sub, "_assignment_id", "")))
        due = parse_dt(getattr(assignment, "due", None)) if assignment else None
        submitted = parse_dt(getattr(sub, "created", None)) or parse_dt(getattr(sub, "submitted", None))
        if not due or not submitted:
            continue
        delta = due - submitted
        deltas.append(delta)
        on_time_flags.append(submitted <= due and not getattr(sub, "late", False))
        if delta >= timedelta(hours=48):
            early_birds += 1
        if submitted > due or getattr(sub, "late", False):
            late_submissions += 1

    avg_procrastination = None
    if deltas:
        avg_procrastination = sum(deltas, timedelta()) / len(deltas)

    # Missing assignments
    missing = 0
    missing_per_course = defaultdict(int)
    submitted_assignment_ids = {str(getattr(sub, "_assignment_id", "")) for sub in submissions}
    for section in sections:
        assigns = assignments_by_section.get(section.id, [])
        for a in assigns:
            due = parse_dt(getattr(a, "due", None))
            # count as missing if past due and no submission
            if due and due < now and str(getattr(a, "id", "")) not in submitted_assignment_ids:
                missing += 1
                missing_per_course[section.id] += 1

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

    # Attachment stats
    total_files = 0
    max_file_size = 0
    for sub in submissions:
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
            classmate_counts[enr.uid]["sections"].add(getattr(section, "section_title", ""))
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
        if total_hours >= 1:
            return f"{total_hours:.0f} hours"
        return f"{total_hours*60:.0f} minutes"

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

    total_submissions = len(submissions) or 1
    early_pct = round((early_birds / total_submissions) * 100, 1)
    add_slide(
        "Early Bird",
        early_birds,
        f"assignments submitted more than 48 hours early... that's {early_pct}% of assignments!",
    )

    add_slide(
        "Late Ledger",
        late_submissions,
        "late submissions (at least you turned them in eventually?)",
    )

    add_slide(
        "Missing Watch",
        missing,
        "missing assignments (and you didn't turn these ones in...)",
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


if __name__ == "__main__":
    app.run(debug=True, port=5002)
