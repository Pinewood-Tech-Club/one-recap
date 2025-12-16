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
import base64
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
from test_img import (
    render_busiest_month_card,
    render_general_stat_card,
    render_procrast_stat_card,
    render_recap_grid,
    render_top_classmates_card,
)

# Optional dotenv load for local dev
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import schoolopy
import requests_oauthlib
import requests

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

# Initialize databases ---------------------------------------------------
def init_recap_db():
    """Initialize both recaps (permanent) and jobs (temporary queue) tables."""
    conn = sqlite3.connect(JOB_DB_PATH)
    cur = conn.cursor()

    # Recaps table (permanent storage)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS recaps (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE,
            slides_json TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )

    # Jobs table (temporary queue - deleted after completion)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            email TEXT,
            status TEXT,
            access_token TEXT,
            access_token_secret TEXT,
            two_legged INTEGER DEFAULT 0,
            progress_json TEXT,
            created_at TEXT
        )
        """
    )

    conn.commit()
    conn.close()


init_recap_db()


# Database helper functions -------------------------------------------------
def get_conn():
    return sqlite3.connect(JOB_DB_PATH, check_same_thread=False)


# Recap operations (permanent storage)
def get_recap_by_email(email):
    """Get the recap for an email (one per email)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, email, slides_json, created_at, updated_at FROM recaps WHERE email = ?", (email,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "email": row[1],
        "slides": json.loads(row[2]) if row[2] else None,
        "created_at": row[3],
        "updated_at": row[4],
    }


def get_recap_by_id(recap_id):
    """Get a recap by its ID."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, email, slides_json, created_at, updated_at FROM recaps WHERE id = ?", (recap_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "email": row[1],
        "slides": json.loads(row[2]) if row[2] else None,
        "created_at": row[3],
        "updated_at": row[4],
    }


def save_recap(recap_id, email, slides):
    """Save or update a recap (replaces existing for this email)."""
    conn = get_conn()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    # Delete existing recap for this email
    cur.execute("DELETE FROM recaps WHERE email = ?", (email,))
    # Insert new recap
    cur.execute(
        "INSERT INTO recaps (id, email, slides_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (recap_id, email, json.dumps(slides), now, now),
    )
    conn.commit()
    conn.close()


def update_recap_slides(recap_id, slides):
    """Update slides_json for an existing recap."""
    conn = get_conn()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute(
        "UPDATE recaps SET slides_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(slides), now, recap_id),
    )
    conn.commit()
    conn.close()


# Job operations (temporary queue)
def create_job(job_id, email, access_token, access_token_secret, two_legged=False):
    """Create a new job in the queue."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO jobs (id, email, status, access_token, access_token_secret, two_legged, created_at, progress_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (job_id, email, "queued", access_token, access_token_secret, 1 if two_legged else 0, datetime.utcnow().isoformat(), None),
    )
    conn.commit()
    conn.close()


def get_job(job_id):
    """Get a job from the queue."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, email, status, access_token, access_token_secret, two_legged, progress_json FROM jobs WHERE id = ?",
        (job_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "email": row[1],
        "status": row[2],
        "access_token": row[3],
        "access_token_secret": row[4],
        "two_legged": bool(row[5]),
        "progress": json.loads(row[6]) if row[6] else None,
    }


def get_job_by_email(email):
    """Get the active job for an email (if any)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, email, status, progress_json FROM jobs WHERE email = ? LIMIT 1",
        (email,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "email": row[1],
        "status": row[2],
        "progress": json.loads(row[3]) if row[3] else None,
    }


def update_job_progress(job_id, progress):
    """Update job progress."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE jobs SET progress_json = ? WHERE id = ?",
        (json.dumps(progress), job_id),
    )
    conn.commit()
    conn.close()


def delete_job(job_id):
    """Delete a job from the queue (after completion or error)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()
    conn.close()


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


def notify_progress(job_id: str, payload: dict):
    """Notify WebSocket subscribers and persist progress."""
    # Push to subscribers
    subs = subscribers.get(job_id, [])
    for q in subs:
        q.put(payload)
    # Update job progress (not status, since status is only queued/running)
    if payload.get("status") not in ["done", "error"]:
        update_job_progress(job_id, payload)


def get_base_url():
    """Return the externally reachable base URL."""
    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not base_url:
        base_url = request.host_url.rstrip("/")
    return base_url


def get_share_image_url(recap_id: str):
    base_url = get_base_url()
    return f"{base_url}/static/userdata/{recap_id}/grid.png"


def fetch_user_profile(auth, user_id: str | None):
    if not user_id:
        return {}
    try:
        resp = auth.oauth.get(f"{SCHOOLOGY_API_DOMAIN}/v1/users/{user_id}", timeout=10)
        if resp.status_code == 200:
            return resp.json() or {}
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to fetch user profile for %s: %s", user_id, exc)
    return {}


def fetch_avatar_data_uri(auth, avatar_url: str | None):
    if not avatar_url:
        return None

    session = getattr(auth, "oauth", None)
    try:
        resp = session.get(avatar_url, timeout=10) if session else requests.get(avatar_url, timeout=10)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Avatar fetch failed for %s: %s", avatar_url, exc)
        return None

    if not resp or resp.status_code >= 400:
        logger.warning("Avatar fetch returned status %s for %s", getattr(resp, "status_code", "?"), avatar_url)
        return None

    data = resp.content or b""
    content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    is_svg = "svg" in content_type or avatar_url.lower().endswith(".svg")
    media_type = content_type or "image/png"

    if is_svg:
        try:
            import cairosvg

            data = cairosvg.svg2png(bytestring=data)
            media_type = "image/png"
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("SVG to PNG conversion failed for %s: %s", avatar_url, exc)
            media_type = "image/svg+xml"

    try:
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{media_type};base64,{encoded}"
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Avatar encoding failed for %s: %s", avatar_url, exc)
        return None


def send_recap_email(email: str | None, job_id: str):
    """Send recap-ready email via SES when configured, else log to console."""
    if not email:
        return

    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    recap_link = f"{base_url}/recap/{job_id}" if base_url else f"/recap/{job_id}"

    ses_region = os.environ.get("AWS_SES_REGION")
    ses_sender = os.environ.get("AWS_SES_SENDER")
    aws_key = os.environ.get("AWS_ACCESS_KEY_ID")
    aws_secret = os.environ.get("AWS_SECRET_ACCESS_KEY")

    if ses_region and ses_sender and aws_key and aws_secret:
        try:
            import boto3

            ses_client = boto3.client(
                "ses",
                region_name=ses_region,
                aws_access_key_id=aws_key,
                aws_secret_access_key=aws_secret,
            )
            ses_client.send_email(
                Source=ses_sender,
                Destination={"ToAddresses": [email]},
                Message={
                    "Subject": {"Data": "Your Schoology recap is ready"},
                    "Body": {
                        "Text": {
                            "Data": f"Your recap is ready. View it here: {recap_link}",
                        }
                    },
                },
            )
            logger.info("SES email sent to %s for recap %s", email, job_id)
            return
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("SES email failed; falling back to log: %s", exc)

    # Fallback: log/print if SES not configured
    logger.info("Recap ready for %s; link: %s", email, recap_link)


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
            # Save to recaps table
            save_recap(job_id, job["email"], slides)
            # Notify completion
            notify_progress(job_id, {"status": "done", "slides": slides})
            # Delete job from queue (OAuth tokens deleted)
            delete_job(job_id)
            send_recap_email(job["email"], job_id)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Job %s failed", job_id)
            # Notify error
            notify_progress(job_id, {"status": "error", "error": str(exc)})
            # Delete job from queue (don't leave failed jobs)
            delete_job(job_id)


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


def _to_float(val, default=0.0):
    try:
        return float(val)
    except Exception:
        return default


def generate_share_images(slides: dict, recap_id: str):
    """Generate shareable recap grid image and stash path in slides."""
    static_root = app.static_folder or os.path.join(app.root_path, "static")
    out_dir = os.path.join(static_root, "userdata", recap_id)
    os.makedirs(out_dir, exist_ok=True)

    try:
        data = {
            "total_assignments": slides.get("total_assignments", 0),
            "course_count": slides.get("total_courses", slides.get("course_count", 0)),
            "late_night_submissions": slides.get("night_owl_subs", 0),
            "busiest_month": slides.get("busiest_month", ""),
            "busiest_month_assignments": slides.get("assignments_bm", 0),
            "weekend_submissions": slides.get("weekend_subs", 0),
            "avg_hours_before_deadline": _to_float(slides.get("avg_procrastination", 0.0)),
            "top_classmates": [
                {
                    "name": c.get("name", ""),
                    "detail": f"{c.get('count', 0)} shared classes",
                    "sections": c.get("sections", []),
                }
                for c in (slides.get("top_classmates") or [])[:3]
            ],
        }

        grid_path = os.path.join(out_dir, "grid.png")
        static_title_path = os.path.join(static_root, "Slide_center-title.png")
        static_cta_path = os.path.join(static_root, "Slide_CTA.png")
        render_recap_grid(
            grid_path,
            data,
            static_title_path=static_title_path,
            static_cta_path=static_cta_path,
        )

        # Per-slide images mapped to slide indices (0 = title, so start at 1)
        slide_images = {}
        try:
            slide_images[1] = f"/static/userdata/{recap_id}/slide-1.png"
            render_general_stat_card(
                os.path.join(out_dir, "slide-1.png"),
                data["total_assignments"],
                "I had",
                "assignments in Schoology",
                small_text=f"across {data.get('course_count', 0)} courses",
                background=(15, 23, 42),
                foreground=(226, 232, 240),
                accent=(34, 211, 238),
                size=1080,
            )

            slide_images[2] = f"/static/userdata/{recap_id}/slide-2.png"
            render_busiest_month_card(
                os.path.join(out_dir, "slide-2.png"),
                data.get("busiest_month", "October"),
                detail_text=f"With {data.get('busiest_month_assignments', 0)} assignments",
                size=1080,
            )

            slide_images[3] = f"/static/userdata/{recap_id}/slide-3.png"
            render_general_stat_card(
                os.path.join(out_dir, "slide-3.png"),
                data.get("weekend_submissions", 0),
                "I submitted",
                "assignments to Schoology",
                small_text="on weekends",
                background=(10, 22, 37),
                foreground=(226, 232, 240),
                accent=(34, 211, 238),
                size=1080,
            )

            slide_images[4] = f"/static/userdata/{recap_id}/slide-4.png"
            render_general_stat_card(
                os.path.join(out_dir, "slide-4.png"),
                data.get("weekday_submissions", data.get("weekday_subs", 0)),
                "I submitted",
                "assignments to Schoology",
                small_text="on weekdays",
                background=(12, 23, 40),
                foreground=(226, 232, 240),
                accent=(34, 211, 238),
                size=1080,
            )

            slide_images[5] = f"/static/userdata/{recap_id}/slide-5.png"
            render_procrast_stat_card(
                os.path.join(out_dir, "slide-5.png"),
                data.get("avg_hours_before_deadline", data.get("avg_procrastination", 0.0)),
                background=(237, 110, 102),
                foreground=(255, 255, 255),
                accent=(253, 224, 71),
                size=1080,
            )

            slide_images[6] = f"/static/userdata/{recap_id}/slide-6.png"
            render_general_stat_card(
                os.path.join(out_dir, "slide-6.png"),
                data.get("late_night_submissions", data.get("night_owl_subs", 0)),
                "I submitted",
                "assignments to Schoology",
                small_text="past 10pm",
                background=(12, 23, 40),
                foreground=(226, 232, 240),
                accent=(34, 211, 238),
                size=1080,
            )

            slide_images[7] = f"/static/userdata/{recap_id}/slide-7.png"
            render_top_classmates_card(
                os.path.join(out_dir, "slide-7.png"),
                data.get("top_classmates", []),
                background=(20, 21, 35),
                foreground=(230, 234, 240),
                accent=(14, 165, 233),
                size=1080,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Failed to render per-slide images for %s: %s", recap_id, exc)

        slides["share_images"] = {
            "grid": f"/static/userdata/{recap_id}/grid.png",
            "slides": slide_images,
        }
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to generate share image for recap %s: %s", recap_id, exc)

    return slides


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
    profile_data = fetch_user_profile(auth, user_id)
    avatar_source_url = (
        profile_data.get("picture_url")
        or profile_data.get("picture")
        or profile_data.get("pic_url")
        or (getattr(me, "picture_url", "") if me else "")
    )
    avatar_data_uri = fetch_avatar_data_uri(auth, avatar_source_url)

    schoology_user = {
        "id": user_id,
        "name": profile_data.get("name_display")
        or profile_data.get("name")
        or (getattr(me, "name_display", "") if me else ""),
        "email": user_email or profile_data.get("primary_email") or (getattr(me, "primary_email", "") if me else ""),
        "avatar": avatar_data_uri or avatar_source_url or "",
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
        if submitted.hour >= 22 or submitted.hour < 6:
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

        is_late_flag = bool(getattr(sub, "late", False))
        is_late = (submitted and due and submitted > due) or is_late_flag
        is_on_time = not is_late
        on_time_flags.append(is_on_time)

        if is_late:
            # Late work counts as zero hours early for average procrastination
            deltas.append(timedelta())
            late_submissions += 1
            continue

        if due:
            delta = due - submitted
            deltas.append(delta)
            if delta >= timedelta(hours=48):
                early_birds += 1

    avg_procrastination = None
    if deltas:
        avg_procrastination = sum(deltas, timedelta()) / len(deltas)

    # Classroom constants (top classmates by shared sections)
    classmate_counts = defaultdict(lambda: {"count": 0, "sections": set(), "name": ""})
    for section in sections:
        enrolls = section_enrollments.get(section.id, [])
        for enr in enrolls:
            if str(getattr(enr, "uid", "")) == str(user_id):
                continue
            classmate_counts[enr.uid]["count"] += 1
            classmate_counts[enr.uid]["sections"].add(f'{getattr(section, "course_title", "")}: {getattr(section, "section_title", "")}')
            classmate_counts[enr.uid]["name"] = getattr(enr, "name_display", f"User {enr.uid}")

    top_classmates = sorted(classmate_counts.items(), key=lambda x: x[1]["count"], reverse=True)[:5]

    # Helper function for formatting time deltas
    def format_delta(td: timedelta):
        total_hours = td.total_seconds() / 3600
        return f"{total_hours:.1f}"

    # Calculate total assignments
    total_assignments = sum(len(assigns) for assigns in assignments_by_section.values())

    # Return computed variables for frontend to use with recap-style.json
    slides = {
        # Basic counts
        "total_assignments": total_assignments,
        "total_courses": len(sections),
        "course_count": len(sections),
        "user_name": schoology_user.get("name", ""),
        "user_avatar": schoology_user.get("avatar", ""),
        "user_email": schoology_user.get("email", ""),

        # Busiest month
        "busiest_month": busiest_month[0] if busiest_month else "",
        "assignments_bm": busiest_month[1] if busiest_month else 0,

        # Submission timing
        "weekend_subs": weekend_subs,
        "weekday_subs": weekday_subs,
        "night_owl_subs": night_owl_subs,
        "night_owl_pct": night_pct,

        # Procrastination metrics
        "avg_procrastination": format_delta(avg_procrastination) if avg_procrastination else "0",
        "early_birds": early_birds,
        "early_bird_pct": round((early_birds / (len(latest_submissions) or 1)) * 100, 1),
        "late_submissions": late_submissions,
        "late_pct": round((late_submissions / (len(latest_submissions) or 1)) * 100, 1),

        # Top courses
        "top_assignment_course": getattr(section_lookup.get(top_assignment_course[0]), "course_title", "") if top_assignment_course else "",
        "top_assignment_count": top_assignment_course[1] if top_assignment_course else 0,

        # Top classmates
        "top_classmates": [
            {
                "name": c["name"],
                "count": c["count"],
                "sections": list(c["sections"]),
            }
            for _, c in top_classmates
        ],
    }
    # Generate shareable images
    return generate_share_images(slides, job_id)


# Routes --------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/auth/start")
def auth_start():
    """Kick off Schoology OAuth."""
    if TWO_LEGGED_DEBUG:
        # Store debug credentials in session
        session["email"] = DEBUG_EMAIL
        session["access_token"] = SCHOOLOGY_CONSUMER_KEY
        session["access_token_secret"] = SCHOOLOGY_CONSUMER_SECRET
        session["two_legged"] = True
        return redirect("/recap")

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

    # Store email and tokens in session
    session["email"] = email
    session["access_token"] = access_token
    session["access_token_secret"] = access_token_secret

    # Redirect to /recap (no parameters)
    return redirect("/recap")


@app.route("/recap")
def recap_index():
    """Landing page for /recap - checks for existing recap or starts new job."""
    email = session.get("email")
    if not email:
        return redirect("/")  # No auth, go to landing

    # Check for existing completed recap
    existing_recap = get_recap_by_email(email)

    # Check for in-progress job
    active_job = get_job_by_email(email)

    if existing_recap and not active_job:
        # Has completed recap, no job in progress - show existing screen
        return render_template("recap.html",
                             recap_id=existing_recap["id"],
                             email=email,
                             share_image_url=get_share_image_url(existing_recap["id"]),
                             show_existing=True,
                             is_generating=False)
    elif active_job:
        # Job in progress - redirect to job URL
        return redirect(f"/recap/{active_job['id']}")
    else:
        # No recap, no job - create new job and redirect
        job_id = str(uuid.uuid4())
        access_token = session.get("access_token")
        access_token_secret = session.get("access_token_secret")
        two_legged = session.get("two_legged", False)
        create_job(job_id, email, access_token, access_token_secret, two_legged=two_legged)
        return redirect(f"/recap/{job_id}")


@app.route("/recap/<recap_id>")
def recap_view(recap_id):
    """View specific recap (generating or completed)."""
    # Check if this is an active job
    job = get_job(recap_id)
    if job:
        # Job in progress
        return render_template("recap.html",
                             recap_id=recap_id,
                             email=job["email"],
                             share_image_url=get_share_image_url(recap_id),
                             show_existing=False,
                             is_generating=True)

    # Check if this is a completed recap
    recap = get_recap_by_id(recap_id)
    if recap:
        # Completed recap
        return render_template("recap.html",
                             recap_id=recap_id,
                             email=recap["email"],
                             share_image_url=get_share_image_url(recap_id),
                             show_existing=False,
                             is_generating=False)

    # Not found
    return "Recap not found", 404


@app.route("/api/recap/<recap_id>")
def get_recap_api(recap_id):
    """Get a completed recap by ID."""
    recap = get_recap_by_id(recap_id)
    if not recap:
        return jsonify({"error": "not_found"}), 404
    slides = recap.get("slides") or {}
    grid_rel = (slides.get("share_images") or {}).get("grid")
    grid_abs = None
    if grid_rel and grid_rel.startswith("/"):
        grid_abs = os.path.join(app.root_path, grid_rel.lstrip("/"))
    if not grid_rel or not grid_abs or not os.path.exists(grid_abs):
        slides = generate_share_images(slides, recap_id)
        recap["slides"] = slides
        update_recap_slides(recap_id, slides)
    return jsonify(recap)


@app.route("/api/recap/delete", methods=["POST"])
def delete_recap_by_email():
    """Delete recap by email (for regeneration)."""
    email = session.get("email")
    if not email:
        return jsonify({"error": "not_authenticated"}), 401

    # Delete the existing recap for this email
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM recaps WHERE email = ?", (email,))
    conn.commit()
    conn.close()

    return jsonify({"success": True})


# WebSocket for live progress updates
@sock.route("/ws/job/<job_id>")
def job_ws(ws, job_id):
    # Send initial state
    job = get_job(job_id)
    if job:
        initial_state = {
            "status": job["status"],
            "progress": job["progress"],
        }
        ws.send(json.dumps(initial_state))

    # Subscribe to updates
    q = queue.Queue()
    subscribers.setdefault(job_id, []).append(q)

    try:
        while True:
            payload = q.get()
            ws.send(json.dumps(payload))
    except Exception:
        pass
    finally:
        # Cleanup
        subs = subscribers.get(job_id, [])
        if q in subs:
            subs.remove(q)


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/s/<username>")
def shared_recap(username):
    """Shared recap view for username (e.g., /s/28axu for 28axu@pinewood.edu)."""
    # Construct email from username
    email = f"{username}@pinewood.edu"

    # Look up recap by email
    recap = get_recap_by_email(email)
    if not recap or not recap.get("slides"):
        return "Recap not found", 404

    slides = recap["slides"] or {}
    grid_rel = (slides.get("share_images") or {}).get("grid")
    grid_abs = None
    if grid_rel and grid_rel.startswith("/"):
        grid_abs = os.path.join(app.root_path, grid_rel.lstrip("/"))
    if not grid_rel or not grid_abs or not os.path.exists(grid_abs):
        slides = generate_share_images(slides, recap["id"])
        update_recap_slides(recap["id"], slides)
        recap["slides"] = slides

    # Build recap URL for iframe
    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not base_url:
        base_url = request.host_url.rstrip("/")
    recap_url = f"{base_url}/recap/{recap['id']}"

    # Render shared recap template
    return render_template(
        "shared-recap.html",
        user_name=slides.get("user_name", ""),
        user_email=email,
        total_assignments=slides.get("total_assignments", 0),
        total_courses=slides.get("total_courses", 0),
        recap_url=recap_url,
        share_image_url=slides.get("share_images", {}).get("grid") and f"{base_url}{slides.get('share_images', {}).get('grid')}"
        or f"{base_url}/static/recap-card.png",
    )


if __name__ == "__main__":
    app.run(debug=True, port=5002)
