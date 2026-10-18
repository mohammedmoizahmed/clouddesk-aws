import logging
import os
from contextlib import closing

import boto3
import pymysql
from botocore.exceptions import BotoCoreError, ClientError
from flask import Flask, redirect, render_template, request
from werkzeug.utils import secure_filename

BUCKET = os.environ.get("S3_BUCKET", "clouddesk-attachments-moiz-001")
STATUSES = ["Open", "In Progress", "Resolved"]

MESSAGES = {
    "created": ("success", "Ticket created successfully."),
    "uploaded": ("success", "Ticket created. Your screenshot was uploaded and will be checked automatically."),
    "updated": ("success", "Ticket status updated."),
    "toolarge": ("error", "That file is too large. The limit is 6 MB."),
    "uploadfail": ("error", "The ticket was saved, but the screenshot could not be uploaded."),
}

handlers = [logging.StreamHandler()]
if os.path.isdir("/data"):
    handlers.append(logging.FileHandler("/data/app.log"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=handlers)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024
s3 = boto3.client("s3", region_name="us-east-1")


def db():
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ.get("DB_NAME", "clouddesk"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=5,
    )


def init():
    with closing(db()) as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS tickets (
            id INT AUTO_INCREMENT PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            description TEXT,
            status VARCHAR(20) NOT NULL DEFAULT 'Open',
            attachment VARCHAR(512),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")


@app.route("/health")
def health():
    return "OK", 200


@app.route("/")
def index():
    with closing(db()) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, title, description, status, attachment, created_at "
                    "FROM tickets ORDER BY id DESC")
        tickets = cur.fetchall()
        cur.execute("SELECT status, COUNT(*) AS n FROM tickets GROUP BY status")
        counts = {s: 0 for s in STATUSES}
        for row in cur.fetchall():
            if row["status"] in counts:
                counts[row["status"]] = row["n"]
    message = MESSAGES.get(request.args.get("m"))
    return render_template("index.html", tickets=tickets, statuses=STATUSES,
                           counts=counts, total=len(tickets), message=message)


@app.route("/tickets", methods=["POST"])
def create_ticket():
    title = request.form["title"].strip()[:255]
    description = request.form.get("description", "").strip()
    with closing(db()) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO tickets (title, description) VALUES (%s, %s)",
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
        with closing(db()) as conn, conn.cursor() as cur:
            cur.execute("UPDATE tickets SET attachment = %s WHERE id = %s", (key, ticket_id))
        logging.info("Ticket %s created with attachment %s", ticket_id, key)
        return redirect("/?m=uploaded")

    logging.info("Ticket %s created", ticket_id)
    return redirect("/?m=created")


@app.route("/tickets/<int:ticket_id>/status", methods=["POST"])
def update_status(ticket_id):
    status = request.form["status"]
    if status in STATUSES:
        with closing(db()) as conn, conn.cursor() as cur:
            cur.execute("UPDATE tickets SET status = %s WHERE id = %s", (status, ticket_id))
        logging.info("Ticket %s set to %s", ticket_id, status)
    return redirect("/?m=updated")


@app.errorhandler(413)
def too_large(e):
    return redirect("/?m=toolarge")


try:
    init()
except Exception:
    logging.exception("Could not initialise the database at startup")
