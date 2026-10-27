import hmac
import logging
import math
import os
import re
import secrets
from datetime import timedelta
from urllib.parse import urlparse

import boto3
import pymysql
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

APP_VERSION = "4"
REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET = os.environ.get("S3_BUCKET", "clouddesk-attachments-moiz-001")
ATTACH_TABLE = os.environ.get("ATTACH_TABLE", "clouddesk-attachments")

STATES = ["New", "In Progress", "On Hold", "Resolved", "Closed"]
PRIORITIES = ["Critical", "High", "Moderate", "Low"]
CATEGORIES = ["Hardware", "Software", "Network", "Email", "Access", "Printer", "Other"]
PER_PAGE = 15

# Roles. Requesters see their own incidents, agents work all incidents, and administrators can also
# manage users and permanently delete things.
ROLES = ["requester", "agent", "administrator"]
ROLE_LABELS = {"requester": "Requester", "agent": "Agent", "administrator": "Administrator"}
STAFF = ("agent", "administrator")

# File types the Lambda checker accepts. The type is set from the extension, never from what the
# uploader claims, so files are shown inline as images/PDF/text instead of being downloaded.
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
        ".pdf": "application/pdf", ".txt": "text/plain"}

MESSAGES = {
    "toolarge": "That file is too large. The limit is 6 MB.",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("clouddesk")

app = Flask(__name__)
_secret = os.environ.get("SECRET_KEY")
if not _secret:
    raise RuntimeError("SECRET_KEY is not set")
app.config.update(
    SECRET_KEY=_secret,
    MAX_CONTENT_LENGTH=6 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

s3 = boto3.client("s3", region_name=REGION, config=Config(signature_version="s3v4"))
ddb = boto3.resource("dynamodb", region_name=REGION).Table(ATTACH_TABLE)

DUMMY_HASH = generate_password_hash("not-a-real-password")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9\s\-()]{6,19}$")


# ---------------------------------------------------------------- database
def connect():
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ.get("DB_NAME", "clouddesk"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=5,
    )


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def q(sql, args=(), one=False):
    with db().cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone() if one else cur.fetchall()


def x(sql, args=()):
    with db().cursor() as cur:
        cur.execute(sql, args)
        return cur.lastrowid


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        full_name VARCHAR(120) NOT NULL,
        email VARCHAR(190) NOT NULL UNIQUE,
        phone VARCHAR(30) NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        role VARCHAR(20) NOT NULL DEFAULT 'requester',
        active TINYINT(1) NOT NULL DEFAULT 1,
        must_change_password TINYINT(1) NOT NULL DEFAULT 0,
        session_version INT NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS tickets (
        id INT AUTO_INCREMENT PRIMARY KEY,
        ticket_number VARCHAR(20) NULL UNIQUE,
        title VARCHAR(255) NOT NULL,
        description TEXT,
        status VARCHAR(20) NOT NULL DEFAULT 'New',
        category VARCHAR(40) NOT NULL DEFAULT 'General',
        priority VARCHAR(20) NOT NULL DEFAULT 'Moderate',
        attachment VARCHAR(512),
        requester_id INT NULL,
        assignee_id INT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS comments (
        id INT AUTO_INCREMENT PRIMARY KEY,
        ticket_id INT NOT NULL,
        user_id INT NOT NULL,
        body TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX (ticket_id))""",
    """CREATE TABLE IF NOT EXISTS audit_log (
        id INT AUTO_INCREMENT PRIMARY KEY,
        actor_id INT NULL,
        actor_name VARCHAR(120) NOT NULL,
        action VARCHAR(60) NOT NULL,
        detail VARCHAR(500) NOT NULL DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX (created_at))""",
]

# Columns added after version 1. Applied only if an older database is missing them.
UPGRADES = [
    "ALTER TABLE tickets ADD COLUMN ticket_number VARCHAR(20) NULL",
    "ALTER TABLE tickets ADD COLUMN category VARCHAR(40) NOT NULL DEFAULT 'General'",
    "ALTER TABLE tickets ADD COLUMN priority VARCHAR(20) NOT NULL DEFAULT 'Moderate'",
    "ALTER TABLE tickets ADD COLUMN requester_id INT NULL",
    "ALTER TABLE tickets ADD COLUMN assignee_id INT NULL",
    "ALTER TABLE tickets ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP "
    "ON UPDATE CURRENT_TIMESTAMP",
    "ALTER TABLE users ADD COLUMN active TINYINT(1) NOT NULL DEFAULT 1",
    "ALTER TABLE users ADD COLUMN must_change_password TINYINT(1) NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN session_version INT NOT NULL DEFAULT 0",
]
IGNORABLE = {1060, 1061}  # duplicate column name, duplicate key name


def init_db():
    conn = connect()
    try:
        with conn.cursor() as cur:
            for stmt in SCHEMA:
                cur.execute(stmt)
            for stmt in UPGRADES:
                try:
                    cur.execute(stmt)
                except pymysql.err.OperationalError as exc:
                    if exc.args[0] not in IGNORABLE:
                        raise
            # Add the unique index only if there is none yet. (Adding it blindly on every start would
            # create a new duplicate index each time, because MySQL does not complain about that.)
            cur.execute("SHOW INDEX FROM tickets WHERE Column_name = 'ticket_number'")
            if not cur.fetchall():
                try:
                    cur.execute("ALTER TABLE tickets ADD UNIQUE KEY uq_ticket_number (ticket_number)")
                except pymysql.err.OperationalError as exc:
                    if exc.args[0] not in IGNORABLE:
                        raise
            cur.execute("UPDATE tickets SET ticket_number = CONCAT('INC', LPAD(1000 + id, 7, '0')) "
                        "WHERE ticket_number IS NULL")
            cur.execute("UPDATE tickets SET status = 'New' WHERE status = 'Open'")
    finally:
        conn.close()


# ---------------------------------------------------------------- helpers
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def fmt(value, short=False):
    if not value:
        return "-"
    return value.strftime("%d %b %H:%M" if short else "%d %b %Y %H:%M")


def qs(**changes):
    """Current query string with some values changed or removed (used for sorting and paging)."""
    args = {k: v for k, v in request.args.items() if k not in changes}
    for key, value in changes.items():
        if value not in (None, ""):
            args[key] = value
    return url_for(request.endpoint, **args)


@app.template_filter("slug")
def slug(value):
    return str(value).lower().replace(" ", "-")


@app.template_filter("initials")
def initials(name):
    parts = [p for p in str(name).split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


app.jinja_env.globals.update(csrf_token=csrf_token, fmt=fmt, qs=qs)


@app.template_filter("rolelabel")
def rolelabel(role):
    return ROLE_LABELS.get(role, str(role).title())


@app.context_processor
def inject():
    return dict(STATES=STATES, PRIORITIES=PRIORITIES, CATEGORIES=CATEGORIES, ROLES=ROLES,
                ROLE_LABELS=ROLE_LABELS, APP_VERSION=APP_VERSION, staff=is_staff(), admin=is_admin())


def safe_next(target):
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return None
    return target


def is_staff():
    """Agents and administrators."""
    return bool(g.get("user")) and g.user["role"] in STAFF


def is_admin():
    return bool(g.get("user")) and g.user["role"] == "administrator"


def admin_required():
    if not is_admin():
        abort(403)


def clean_filename(original):
    base, ext = os.path.splitext(original or "")
    ext = ext.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", ext):
        ext = ""
    base = secure_filename(base)[:80] or "attachment"
    return base + ext


def attachment_info(key):
    """What the Lambda checker decided about a file, plus a short-lived viewing link if it passed."""
    mime = MIME.get(os.path.splitext(key)[1].lower())
    info = {"name": key.split("/", 1)[-1], "status": "unknown", "reason": "", "url": None,
            "kind": ("image" if mime and mime.startswith("image/") else "file") if mime else None}
    try:
        item = ddb.get_item(Key={"file_key": key}).get("Item")
    except (BotoCoreError, ClientError):
        return info
    if not item:
        info["status"] = "checking"
        return info
    info["status"] = item.get("status", "unknown")
    info["reason"] = item.get("reason", "")
    if info["status"] == "accepted" and mime:
        try:
            info["url"] = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": BUCKET, "Key": key,
                        "ResponseContentType": mime, "ResponseContentDisposition": "inline"},
                ExpiresIn=900)
        except (BotoCoreError, ClientError):
            pass
    return info


# ---------------------------------------------------------------- request hooks
PUBLIC = {"login", "health", "static"}
# Pages a person may use while they are required to choose a new password.
PASSWORD_CHANGE_OK = {"profile", "change_password", "logout"}


@app.before_request
def guard():
    g.user = None
    if request.endpoint in ("health", "static"):
        return
    uid = session.get("uid")
    if uid:
        user = q("SELECT id, full_name, email, phone, role, active, must_change_password, session_version "
                 "FROM users WHERE id=%s", (uid,), one=True)
        if user and user["active"] and session.get("sv") == user["session_version"]:
            g.user = user
        else:
            disabled = bool(user) and not user["active"]
            session.clear()
            if disabled:
                flash("Your account has been disabled. Contact your administrator.", "error")
    if request.method == "POST":
        sent = request.form.get("csrf_token", "").encode()
        expected = session.get("csrf", "").encode()
        if not sent or not hmac.compare_digest(sent, expected):
            abort(400)
    if request.endpoint not in PUBLIC and not g.user:
        target = request.full_path.rstrip("?") if request.method == "GET" else None
        return redirect(url_for("login", next=target) if target and target != "/" else url_for("login"))
    if g.user and g.user["must_change_password"] and request.endpoint not in PASSWORD_CHANGE_OK:
        flash("Please choose a new password before you continue.", "info")
        return redirect(url_for("profile"))


@app.after_request
def headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.endpoint != "static":
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- health
@app.route("/health")
def health():
    return "OK", 200


# ---------------------------------------------------------------- accounts
@app.route("/")
def root():
    return redirect(url_for("home"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("home"))
    nxt = request.args.get("next", "")
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = q("SELECT id, password_hash, active, session_version FROM users WHERE email=%s", (email,), one=True)
        if user:
            ok = check_password_hash(user["password_hash"], password)
        else:
            check_password_hash(DUMMY_HASH, password)  # keep timing similar for unknown emails
            ok = False
        if ok and not user["active"]:
            flash("This account is disabled. Contact your administrator.", "error")
        elif ok:
            target = safe_next(request.form.get("next", ""))
            session.clear()
            session["uid"] = user["id"]
            session["sv"] = user["session_version"]
            session.permanent = True
            csrf_token()
            return redirect(target or url_for("home"))
        else:
            flash("Incorrect email or password.", "error")
    return render_template("login.html", next=nxt)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("login"))


@app.route("/profile")
def profile():
    stats = q("SELECT COUNT(*) AS total, SUM(status IN ('Resolved','Closed')) AS done "
              "FROM tickets WHERE requester_id=%s", (g.user["id"],), one=True)
    return render_template("profile.html", total=stats["total"], done=int(stats["done"] or 0))


@app.route("/profile/password", methods=["POST"])
def change_password():
    row = q("SELECT password_hash FROM users WHERE id=%s", (g.user["id"],), one=True)
    current = request.form.get("current", "")
    new = request.form.get("new", "")
    confirm = request.form.get("confirm", "")
    if not check_password_hash(row["password_hash"], current):
        flash("Your current password is not correct.", "error")
    elif len(new) < 8:
        flash("The new password must be at least 8 characters.", "error")
    elif new != confirm:
        flash("The two new passwords do not match.", "error")
    elif new == current:
        flash("Choose a password that is different from your current one.", "error")
    else:
        x("UPDATE users SET password_hash=%s, must_change_password=0, session_version=session_version+1 "
          "WHERE id=%s", (generate_password_hash(new), g.user["id"]))
        session["sv"] = q("SELECT session_version FROM users WHERE id=%s", (g.user["id"],), one=True)["session_version"]
        flash("Your password has been changed.", "success")
    return redirect(url_for("profile"))


# ---------------------------------------------------------------- incidents
SELECT_LIST = (
    "SELECT t.id, COALESCE(t.ticket_number, CONCAT('INC', LPAD(1000 + t.id, 7, '0'))) AS number, "
    "t.title, t.status, t.category, t.priority, t.created_at, t.updated_at, "
    "r.full_name AS requester, a.full_name AS assignee "
    "FROM tickets t LEFT JOIN users r ON r.id = t.requester_id "
    "LEFT JOIN users a ON a.id = t.assignee_id"
)

SORTS = {
    "number": "t.id",
    "short": "t.title",
    "category": "t.category",
    "priority": "FIELD(t.priority,'Critical','High','Moderate','Low')",
    "state": "FIELD(t.status,'New','In Progress','On Hold','Resolved','Closed')",
    "updated": "t.updated_at",
}


def scope_clause():
    if is_staff():
        return "1=1", []
    return "t.requester_id=%s", [g.user["id"]]


@app.route("/home")
def home():
    where, args = scope_clause()
    by_state = {s: 0 for s in STATES}
    total = 0
    for row in q(f"SELECT t.status, COUNT(*) AS n FROM tickets t WHERE {where} GROUP BY t.status", args):
        total += row["n"]
        if row["status"] in by_state:
            by_state[row["status"]] += row["n"]
    by_priority = {p: 0 for p in PRIORITIES}
    for row in q(f"SELECT t.priority, COUNT(*) AS n FROM tickets t WHERE {where} "
                 "AND t.status NOT IN ('Resolved','Closed') GROUP BY t.priority", args):
        if row["priority"] in by_priority:
            by_priority[row["priority"]] += row["n"]
    recent = q(f"{SELECT_LIST} WHERE {where} ORDER BY t.updated_at DESC, t.id DESC LIMIT 8", args)
    return render_template("home.html", total=total, by_state=by_state, by_priority=by_priority,
                           top=max(by_priority.values()) or 1, recent=recent,
                           default_scope="all" if is_staff() else "mine")


@app.route("/incidents")
def incidents():
    agent = is_staff()
    scope = request.args.get("scope", "mine")
    if scope not in ("mine", "assigned", "all") or (not agent and scope != "mine"):
        scope = "mine"
    where, args = [], []
    if scope == "mine":
        where.append("t.requester_id=%s")
        args.append(g.user["id"])
    elif scope == "assigned":
        where.append("t.assignee_id=%s")
        args.append(g.user["id"])
    state = request.args.get("state", "")
    if state in STATES:
        where.append("t.status=%s")
        args.append(state)
    priority = request.args.get("priority", "")
    if priority in PRIORITIES:
        where.append("t.priority=%s")
        args.append(priority)
    text = request.args.get("q", "").strip()[:100]
    if text:
        where.append("(t.ticket_number LIKE %s OR t.title LIKE %s)")
        args += [f"%{text}%", f"%{text}%"]
    clause = " WHERE " + " AND ".join(where) if where else ""

    sort = request.args.get("sort", "updated")
    if sort not in SORTS:
        sort = "updated"
    direction = "asc" if request.args.get("dir") == "asc" else "desc"
    total = q(f"SELECT COUNT(*) AS n FROM tickets t{clause}", args, one=True)["n"]
    pages = max(1, math.ceil(total / PER_PAGE))
    page = request.args.get("page", "1")
    page = min(max(int(page), 1) if page.isdigit() else 1, pages)
    rows = q(f"{SELECT_LIST}{clause} ORDER BY {SORTS[sort]} {direction}, t.id DESC LIMIT %s OFFSET %s",
             args + [PER_PAGE, (page - 1) * PER_PAGE])
    titles = {"mine": "My incidents", "assigned": "Assigned to me", "all": "All incidents"}
    return render_template("incidents.html", rows=rows, total=total, page=page, pages=pages,
                           scope=scope, title=titles[scope], state=state, priority=priority,
                           text=text, sort=sort, direction=direction,
                           first=(page - 1) * PER_PAGE + 1 if total else 0,
                           last=min(page * PER_PAGE, total))


@app.route("/incidents/new", methods=["GET", "POST"])
def incident_new():
    form = {"title": "", "description": "", "category": "Software", "priority": "Moderate"}
    if request.method == "POST":
        title = " ".join(request.form.get("title", "").split())[:255]
        description = request.form.get("description", "").strip()[:5000]
        category = request.form.get("category", "")
        priority = request.form.get("priority", "")
        form = {"title": title, "description": description,
                "category": category if category in CATEGORIES else "Software",
                "priority": priority if priority in PRIORITIES else "Moderate"}
        if not title:
            flash("Enter a short description of the problem.", "error")
            return render_template("incident_new.html", form=form)
        tid = x("INSERT INTO tickets (title, description, status, category, priority, requester_id) "
                "VALUES (%s,%s,'New',%s,%s,%s)",
                (title, description, form["category"], form["priority"], g.user["id"]))
        x("UPDATE tickets SET ticket_number=%s WHERE id=%s", (f"INC{1000 + tid:07d}", tid))
        upload = request.files.get("screenshot")
        if upload and upload.filename:
            key = f"ticket-{tid}/{clean_filename(upload.filename)}"
            content_type = MIME.get(os.path.splitext(key)[1].lower())
            try:
                s3.upload_fileobj(upload, BUCKET, key,
                                  ExtraArgs={"ContentType": content_type} if content_type else None)
            except (BotoCoreError, ClientError):
                log.exception("Upload failed for ticket %s", tid)
                flash("The incident was created, but the attachment could not be uploaded.", "error")
                return redirect(url_for("incident_view", tid=tid))
            x("UPDATE tickets SET attachment=%s WHERE id=%s", (key, tid))
            log.info("Incident %s created with attachment %s", tid, key)
            flash("Incident created. Your attachment is being checked automatically.", "success")
        else:
            log.info("Incident %s created", tid)
            flash("Incident created.", "success")
        return redirect(url_for("incident_view", tid=tid))
    return render_template("incident_new.html", form=form)


def get_incident(tid):
    row = q("SELECT t.id, COALESCE(t.ticket_number, CONCAT('INC', LPAD(1000 + t.id, 7, '0'))) AS number, "
            "t.title, t.description, t.status, t.category, t.priority, t.attachment, "
            "t.requester_id, t.assignee_id, t.created_at, t.updated_at, "
            "r.full_name AS requester, r.email AS requester_email, r.phone AS requester_phone, "
            "a.full_name AS assignee "
            "FROM tickets t LEFT JOIN users r ON r.id = t.requester_id "
            "LEFT JOIN users a ON a.id = t.assignee_id WHERE t.id=%s", (tid,), one=True)
    if not row or (not is_staff() and row["requester_id"] != g.user["id"]):
        abort(404)
    return row


@app.route("/incidents/<int:tid>")
def incident_view(tid):
    row = get_incident(tid)
    comments = q("SELECT c.id, c.body, c.created_at, COALESCE(u.full_name, 'Deleted user') AS full_name, u.role "
                 "FROM comments c "
                 "LEFT JOIN users u ON u.id = c.user_id WHERE c.ticket_id=%s ORDER BY c.id DESC", (tid,))
    agents = q("SELECT id, full_name FROM users WHERE role IN ('agent','administrator') ORDER BY full_name") \
        if is_staff() else []
    att = attachment_info(row["attachment"]) if row["attachment"] else None
    return render_template("incident.html", t=row, comments=comments, agents=agents, att=att)


@app.route("/incidents/<int:tid>/comment", methods=["POST"])
def incident_comment(tid):
    get_incident(tid)
    body = request.form.get("body", "").strip()[:2000]
    body = re.sub(r"^\s*\[system\]", "(system)", body, flags=re.I)
    if not body:
        flash("Write a comment first.", "error")
    else:
        x("INSERT INTO comments (ticket_id, user_id, body) VALUES (%s,%s,%s)", (tid, g.user["id"], body))
        x("UPDATE tickets SET updated_at=NOW() WHERE id=%s", (tid,))
        flash("Comment added.", "success")
    return redirect(url_for("incident_view", tid=tid))


@app.route("/incidents/<int:tid>/update", methods=["POST"])
def incident_update(tid):
    if not is_staff():
        abort(403)
    row = get_incident(tid)
    state = request.form.get("state", row["status"])
    priority = request.form.get("priority", row["priority"])
    assignee = request.form.get("assignee", "")
    if state not in STATES:
        state = row["status"]
    if priority not in PRIORITIES:
        priority = row["priority"]
    assignee_id, assignee_name = None, None
    if assignee.isdigit():
        found = q("SELECT id, full_name FROM users WHERE id=%s AND role IN ('agent','administrator')",
                      (int(assignee),), one=True)
        if found:
            assignee_id, assignee_name = found["id"], found["full_name"]
    notes = []
    if state != row["status"]:
        notes.append(f"State changed from {row['status']} to {state}.")
    if priority != row["priority"]:
        notes.append(f"Priority changed from {row['priority']} to {priority}.")
    if assignee_id != row["assignee_id"]:
        notes.append(f"Assigned to {assignee_name}." if assignee_id else "Assignment removed.")
    if notes:
        x("UPDATE tickets SET status=%s, priority=%s, assignee_id=%s WHERE id=%s",
          (state, priority, assignee_id, tid))
        for note in notes:
            x("INSERT INTO comments (ticket_id, user_id, body) VALUES (%s,%s,%s)",
              (tid, g.user["id"], "[system] " + note))
        flash("Incident updated.", "success")
    else:
        flash("Nothing was changed.", "info")
    return redirect(url_for("incident_view", tid=tid))


# ---------------------------------------------------------------- administration
def audit(action, detail=""):
    x("INSERT INTO audit_log (actor_id, actor_name, action, detail) VALUES (%s,%s,%s,%s)",
      (g.user["id"], g.user["full_name"], action, detail[:500]))
    log.info("AUDIT %s by %s: %s", action, g.user["email"], detail[:200])


def check_user_fields(full_name, email, phone):
    errors = []
    if not (3 <= len(full_name) <= 120) or " " not in full_name:
        errors.append("Enter the full name, first and last name.")
    if not EMAIL_RE.match(email) or len(email) > 190:
        errors.append("Enter a valid email address.")
    if not PHONE_RE.match(phone):
        errors.append("Enter a valid phone number, for example +966 5X XXX XXXX.")
    return errors


def load_user(uid):
    row = q("SELECT id, full_name, email, phone, role, active, must_change_password, created_at "
            "FROM users WHERE id=%s", (uid,), one=True)
    if not row:
        abort(404)
    return row


def purge_incident(ticket_id, attachment):
    """Permanently remove an incident, its comments and its stored file.

    Returns True if the attachment could not be removed from storage."""
    x("DELETE FROM comments WHERE ticket_id=%s", (ticket_id,))
    x("DELETE FROM tickets WHERE id=%s", (ticket_id,))
    leftover = False
    if attachment:
        try:
            s3.delete_object(Bucket=BUCKET, Key=attachment)
        except (BotoCoreError, ClientError):
            log.exception("Could not delete %s from storage", attachment)
            leftover = True
        try:
            ddb.delete_item(Key={"file_key": attachment})
        except (BotoCoreError, ClientError):
            log.exception("Could not delete the check result for %s", attachment)
            leftover = True
    return leftover


STORAGE_WARNING = (" Its attachment could not be fully removed from storage: the web server role needs "
                   "permission to delete from S3 and DynamoDB.")


@app.route("/admin/users")
def admin_users():
    admin_required()
    users = q("SELECT id, full_name, email, phone, role, active, must_change_password, created_at "
              "FROM users ORDER BY full_name")
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/new", methods=["GET", "POST"])
def admin_user_new():
    admin_required()
    form = {"full_name": "", "email": "", "phone": "", "role": "requester", "must_change": True}
    if request.method == "POST":
        full_name = " ".join(request.form.get("full_name", "").split())
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        role = request.form.get("role", "")
        password = request.form.get("password", "")
        must_change = request.form.get("must_change") == "1"
        form = {"full_name": full_name, "email": email, "phone": phone,
                "role": role if role in ROLES else "requester", "must_change": must_change}
        errors = check_user_fields(full_name, email, phone)
        if role not in ROLES:
            errors.append("Choose a role.")
        if len(password) < 8:
            errors.append("The password must be at least 8 characters.")
        if not errors and q("SELECT id FROM users WHERE email=%s", (email,), one=True):
            errors.append("A user with this email already exists.")
        if not errors:
            try:
                x("INSERT INTO users (full_name, email, phone, password_hash, role, must_change_password) "
                  "VALUES (%s,%s,%s,%s,%s,%s)",
                  (full_name, email, phone, generate_password_hash(password), role, 1 if must_change else 0))
            except pymysql.err.IntegrityError:
                errors.append("A user with this email already exists.")
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            audit("user.create", f"{email} as {role}")
            flash(f"User {full_name} has been created.", "success")
            return redirect(url_for("admin_users"))
    return render_template("admin_user_form.html", mode="new", form=form, target=None, is_self=False)


@app.route("/admin/users/<int:uid>", methods=["GET", "POST"])
def admin_user_edit(uid):
    admin_required()
    target = load_user(uid)
    is_self = target["id"] == g.user["id"]
    form = {"full_name": target["full_name"], "email": target["email"], "phone": target["phone"],
            "role": target["role"], "active": bool(target["active"])}
    if request.method == "POST":
        full_name = " ".join(request.form.get("full_name", "").split())
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        role = request.form.get("role", target["role"])
        active = request.form.get("active") == "1"
        if is_self:  # you cannot demote or lock out your own account
            role, active = target["role"], True
        form = {"full_name": full_name, "email": email, "phone": phone, "role": role, "active": active}
        errors = check_user_fields(full_name, email, phone)
        if role not in ROLES:
            errors.append("Choose a role.")
        if not errors and q("SELECT id FROM users WHERE email=%s AND id<>%s", (email, uid), one=True):
            errors.append("Another user already has this email.")
        if not errors:
            try:
                x("UPDATE users SET full_name=%s, email=%s, phone=%s, role=%s, active=%s WHERE id=%s",
                  (full_name, email, phone, role, 1 if active else 0, uid))
            except pymysql.err.IntegrityError:
                errors.append("Another user already has this email.")
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            changes = []
            for label, old, new in (("name", target["full_name"], full_name), ("email", target["email"], email),
                                    ("phone", target["phone"], phone), ("role", target["role"], role),
                                    ("active", bool(target["active"]), active)):
                if old != new:
                    changes.append(f"{label}: {old} -> {new}" if label in ("role", "active") else label)
            if changes:
                audit("user.edit", f"{target['email']}: " + "; ".join(changes))
            flash("User updated." if changes else "Nothing was changed.", "success" if changes else "info")
            return redirect(url_for("admin_user_edit", uid=uid))
    return render_template("admin_user_form.html", mode="edit", form=form, target=target, is_self=is_self)


@app.route("/admin/users/<int:uid>/password", methods=["POST"])
def admin_user_password(uid):
    admin_required()
    target = load_user(uid)
    password = request.form.get("password", "")
    must_change = request.form.get("must_change") == "1"
    if len(password) < 8:
        flash("The password must be at least 8 characters.", "error")
        return redirect(url_for("admin_user_edit", uid=uid))
    x("UPDATE users SET password_hash=%s, must_change_password=%s, session_version=session_version+1 "
      "WHERE id=%s", (generate_password_hash(password), 1 if must_change else 0, uid))
    if uid == g.user["id"]:  # keep the administrator's own session alive
        session["sv"] = q("SELECT session_version FROM users WHERE id=%s", (uid,), one=True)["session_version"]
    audit("user.password_reset", target["email"])
    flash(f"The password for {target['full_name']} has been set. Their other sessions were signed out.", "success")
    return redirect(url_for("admin_user_edit", uid=uid))


@app.route("/admin/users/<int:uid>/delete", methods=["GET", "POST"])
def admin_user_delete(uid):
    admin_required()
    target = load_user(uid)
    if target["id"] == g.user["id"]:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin_user_edit", uid=uid))
    owned = q("SELECT id, attachment FROM tickets WHERE requester_id=%s", (uid,))
    if request.method == "POST":
        leftovers = 0
        removed = 0
        if request.form.get("delete_incidents") == "1":
            for ticket in owned:
                leftovers += 1 if purge_incident(ticket["id"], ticket["attachment"]) else 0
                removed += 1
        x("UPDATE tickets SET assignee_id=NULL WHERE assignee_id=%s", (uid,))
        x("DELETE FROM users WHERE id=%s", (uid,))
        audit("user.delete", f"{target['email']} ({target['role']}); incidents deleted: {removed}")
        flash(f"User {target['full_name']} has been permanently deleted."
              + (f" {removed} of their incidents were deleted too." if removed else "")
              + (STORAGE_WARNING if leftovers else ""), "error" if leftovers else "success")
        return redirect(url_for("admin_users"))
    return render_template("admin_user_delete.html", target=target, owned=len(owned))


@app.route("/admin/audit")
def admin_audit():
    admin_required()
    rows = q("SELECT actor_name, action, detail, created_at FROM audit_log ORDER BY id DESC LIMIT 200")
    return render_template("admin_audit.html", rows=rows)


@app.route("/incidents/<int:tid>/edit", methods=["GET", "POST"])
def incident_edit(tid):
    admin_required()
    t = get_incident(tid)
    users = q("SELECT id, full_name FROM users ORDER BY full_name")
    people = {u["id"] for u in users}
    staff_rows = q("SELECT id, full_name FROM users WHERE role IN ('agent','administrator') ORDER BY full_name")
    staff_ids = {u["id"] for u in staff_rows}
    categories = CATEGORIES if t["category"] in CATEGORIES else CATEGORIES + [t["category"]]
    form = {"title": t["title"], "description": t["description"] or "", "category": t["category"],
            "priority": t["priority"], "status": t["status"],
            "requester": t["requester_id"] or "", "assignee": t["assignee_id"] or ""}
    if request.method == "POST":
        title = " ".join(request.form.get("title", "").split())[:255]
        description = request.form.get("description", "").strip()[:5000]
        category = request.form.get("category", "")
        priority = request.form.get("priority", "")
        status = request.form.get("status", "")
        requester, assignee = request.form.get("requester", ""), request.form.get("assignee", "")
        form = {"title": title, "description": description, "category": category, "priority": priority,
                "status": status, "requester": requester, "assignee": assignee}
        errors = []
        if not title:
            errors.append("Enter a short description.")
        if category not in categories:
            errors.append("Choose a category.")
        if priority not in PRIORITIES:
            errors.append("Choose a priority.")
        if status not in STATES:
            errors.append("Choose a state.")
        requester_id = int(requester) if requester.isdigit() else None
        assignee_id = int(assignee) if assignee.isdigit() else None
        if requester and requester_id not in people:
            errors.append("Choose a caller from the list.")
        if assignee and assignee_id not in staff_ids:
            errors.append("Choose an assignee from the list of agents and administrators.")
        if errors:
            for message in errors:
                flash(message, "error")
        else:
            changes = [label for label, old, new in (
                ("short description", t["title"], title), ("description", t["description"] or "", description),
                ("category", t["category"], category), ("priority", t["priority"], priority),
                ("state", t["status"], status), ("caller", t["requester_id"], requester_id),
                ("assignee", t["assignee_id"], assignee_id)) if old != new]
            x("UPDATE tickets SET title=%s, description=%s, category=%s, priority=%s, status=%s, "
              "requester_id=%s, assignee_id=%s WHERE id=%s",
              (title, description, category, priority, status, requester_id, assignee_id, tid))
            if changes:
                x("INSERT INTO comments (ticket_id, user_id, body) VALUES (%s,%s,%s)",
                  (tid, g.user["id"], "[system] Edited by an administrator: " + ", ".join(changes) + "."))
                audit("incident.edit", f"{t['number']}: " + ", ".join(changes))
            flash("Incident updated." if changes else "Nothing was changed.", "success" if changes else "info")
            return redirect(url_for("incident_view", tid=tid))
    return render_template("incident_edit.html", t=t, form=form, users=users, staff_rows=staff_rows,
                           categories=categories)


@app.route("/incidents/<int:tid>/delete", methods=["GET", "POST"])
def incident_delete(tid):
    admin_required()
    t = get_incident(tid)
    comment_count = q("SELECT COUNT(*) AS n FROM comments WHERE ticket_id=%s", (tid,), one=True)["n"]
    if request.method == "POST":
        leftover = purge_incident(tid, t["attachment"])
        audit("incident.delete", f"{t['number']}: {t['title'][:120]}")
        flash(f"Incident {t['number']} has been permanently deleted." + (STORAGE_WARNING if leftover else ""),
              "error" if leftover else "success")
        return redirect(url_for("incidents", scope="all"))
    return render_template("incident_delete.html", t=t, comment_count=comment_count)


@app.route("/comments/<int:cid>/delete", methods=["POST"])
def comment_delete(cid):
    admin_required()
    row = q("SELECT id, ticket_id FROM comments WHERE id=%s", (cid,), one=True)
    if not row:
        abort(404)
    x("DELETE FROM comments WHERE id=%s", (cid,))
    audit("comment.delete", f"comment {cid} on incident {row['ticket_id']}")
    flash("Comment deleted.", "success")
    return redirect(url_for("incident_view", tid=row["ticket_id"]))


# ---------------------------------------------------------------- errors
def show_error(code, title, message):
    return render_template("error.html", code=code, title=title, message=message), code


@app.errorhandler(400)
def bad_request(_e):
    return show_error(400, "Request could not be verified",
                      "Your session may have expired. Go back, reload the page and try again.")


@app.errorhandler(403)
def forbidden(_e):
    return show_error(403, "Not allowed", "You do not have permission to do that.")


@app.errorhandler(404)
def not_found(_e):
    return show_error(404, "Not found", "The page or incident you asked for does not exist.")


@app.errorhandler(413)
def too_large(_e):
    flash(MESSAGES["toolarge"], "error")
    return redirect(url_for("incident_new") if g.get("user") else url_for("login"))


@app.errorhandler(500)
def server_error(_e):
    return show_error(500, "Something went wrong", "The error has been logged. Please try again shortly.")


try:
    init_db()
except Exception:
    log.exception("Could not initialise the database at startup")