import logging
import sqlite3

import boto3
from flask import Flask, redirect, render_template_string, request
from werkzeug.utils import secure_filename

BUCKET = "clouddesk-attachments-moiz-001"
DB = "/data/tickets.db"
STATUSES = ["Open", "In Progress", "Resolved"]

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


PAGE = """
<!doctype html>
<title>CloudDesk</title>
<style>
  body{font-family:Arial,sans-serif;max-width:900px;margin:30px auto;padding:0 16px}
  input,textarea,select{width:100%;padding:8px;margin:4px 0 12px;box-sizing:border-box}
  button{padding:8px 16px;cursor:pointer}
  table{width:100%;border-collapse:collapse;margin-top:20px}
  th,td{border:1px solid #ccc;padding:8px;text-align:left}
  th{background:#f2f2f2}
  form.inline{display:flex;gap:6px}
  form.inline select{margin:0}
</style>
<h1>CloudDesk: IT Helpdesk</h1>
<h3>New ticket</h3>
<form method="post" action="/tickets" enctype="multipart/form-data">
  <label>Title</label><input name="title" required>
  <label>Description</label><textarea name="description" rows="3"></textarea>
  <label>Screenshot (optional)</label><input type="file" name="screenshot">
  <button type="submit">Create ticket</button>
</form>
<table>
  <tr><th>ID</th><th>Title</th><th>Status</th><th>Attachment</th><th>Created</th><th>Update</th></tr>
  {% for t in tickets %}
  <tr>
    <td>{{ t.id }}</td><td>{{ t.title }}</td><td>{{ t.status }}</td>
    <td>{{ t.attachment or "" }}</td><td>{{ t.created_at }}</td>
    <td>
      <form class="inline" method="post" action="/tickets/{{ t.id }}/status">
        <select name="status">
          {% for s in statuses %}
          <option {% if s == t.status %}selected{% endif %}>{{ s }}</option>
          {% endfor %}
        </select>
        <button>Save</button>
      </form>
    </td>
  </tr>
  {% endfor %}
</table>
"""


@app.route("/")
def index():
    with db() as c:
        tickets = c.execute("SELECT * FROM tickets ORDER BY id DESC").fetchall()
    return render_template_string(PAGE, tickets=tickets, statuses=STATUSES)


@app.route("/tickets", methods=["POST"])
def create_ticket():
    title = request.form["title"].strip()
    description = request.form.get("description", "").strip()
    with db() as c:
        cur = c.execute("INSERT INTO tickets (title, description) VALUES (?, ?)",
                        (title, description))
        ticket_id = cur.lastrowid

    file = request.files.get("screenshot")
    if file and file.filename:
        key = f"ticket-{ticket_id}/{secure_filename(file.filename)}"
        s3.upload_fileobj(file, BUCKET, key)
        with db() as c:
            c.execute("UPDATE tickets SET attachment = ? WHERE id = ?", (key, ticket_id))
        logging.info("Ticket %s created with attachment %s", ticket_id, key)
    else:
        logging.info("Ticket %s created", ticket_id)
    return redirect("/")


@app.route("/tickets/<int:ticket_id>/status", methods=["POST"])
def update_status(ticket_id):
    status = request.form["status"]
    if status in STATUSES:
        with db() as c:
            c.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, ticket_id))
        logging.info("Ticket %s set to %s", ticket_id, status)
    return redirect("/")


init()
