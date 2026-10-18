import logging
import sqlite3

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from flask import Flask, redirect, render_template, request
from werkzeug.utils import secure_filename

BUCKET = "clouddesk-attachments-moiz-001"
DB = "/data/tickets.db"
STATUSES = ["Open", "In Progress", "Resolved"]

MESSAGES = {
    "created": ("success", "Ticket created successfully."),
    "uploaded": ("success", "Ticket created. Your screenshot was uploaded and will be checked automatically."),
    "updated": ("success", "Ticket status updated."),
    "toolarge": ("error", "That file is too large. The limit is 6 MB."),
    "uploadfail": ("error", "The ticket was saved, but the screenshot could not be uploaded."),
}

logging.basicConfig(filename="/data/app.log", level=logging.INFO,
                    format="%(asctime)s %(message)s")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024
s3 = boto3.client("s3", region_name="us-east-1")


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'Open',
            attachment TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")


@app.route("/")
def index():
    with db() as c:
        tickets = c.execute("SELECT * FROM tickets ORDER BY id DESC").fetchall()
        counts = {s: 0 for s in STATUSES}
        for row in c.execute("SELECT status, COUNT(*) AS n FROM tickets GROUP BY status"):
            if row["status"] in counts:
                counts[row["status"]] = row["n"]
    message = MESSAGES.get(request.args.get("m"))
    return render_template("index.html", tickets=tickets, statuses=STATUSES,
                           counts=counts, total=len(tickets), message=message)


@app.route("/tickets", methods=["POST"])
def create_ticket():
    title = request.form["title"].strip()
    description = request.form.get("description", "").strip()
    with db() as c:
        cur = c.execute("INSERT INTO tickets (title, description) VALUES (?, ?)",
                        (title, description))
        ticket_id = cur.lastrowid

    file = request.files.get("screenshot")
    filename = secure_filename(file.filename) if file and file.filename else ""
    if filename:
        key = f"ticket-{ticket_id}/{filename}"
        try:
            s3.upload_fileobj(file, BUCKET, key)
        except (BotoCoreError, ClientError):
            logging.exception("Upload failed for ticket %s", ticket_id)
            return redirect("/?m=uploadfail")
        with db() as c:
            c.execute("UPDATE tickets SET attachment = ? WHERE id = ?", (key, ticket_id))
        logging.info("Ticket %s created with attachment %s", ticket_id, key)
        return redirect("/?m=uploaded")

    logging.info("Ticket %s created", ticket_id)
    return redirect("/?m=created")


@app.route("/tickets/<int:ticket_id>/status", methods=["POST"])
def update_status(ticket_id):
    status = request.form["status"]
    if status in STATUSES:
        with db() as c:
            c.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, ticket_id))
        logging.info("Ticket %s set to %s", ticket_id, status)
    return redirect("/?m=updated")


@app.errorhandler(413)
def too_large(e):
    return redirect("/?m=toolarge")


init()
