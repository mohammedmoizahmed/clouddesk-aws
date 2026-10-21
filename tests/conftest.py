"""Shared test setup.

The tests run against a real MySQL (or MariaDB) database, never production. Point them at a
throwaway server with TEST_DB_HOST, TEST_DB_USER and TEST_DB_PASSWORD. The database name defaults
to clouddesk_test and must contain the word "test" so the suite can never touch real data.
All AWS calls (S3 and DynamoDB) are replaced by in-memory fakes.
"""
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pymysql
import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_DB = os.environ.get("TEST_DB_NAME", "clouddesk_test")
if "test" not in TEST_DB.lower():
    raise RuntimeError("TEST_DB_NAME must contain 'test' so the tests can never touch real data.")

# These deliberately override any real settings in the environment.
os.environ.update(
    DB_HOST=os.environ.get("TEST_DB_HOST", "127.0.0.1"),
    DB_USER=os.environ.get("TEST_DB_USER", "root"),
    DB_PASSWORD=os.environ.get("TEST_DB_PASSWORD", ""),
    DB_NAME=TEST_DB,
    SECRET_KEY="test-secret-key-not-for-production",
    AWS_EC2_METADATA_DISABLED="true",
    AWS_DEFAULT_REGION="us-east-1",
)


def pytest_configure(config):
    try:
        conn = pymysql.connect(host=os.environ["DB_HOST"], user=os.environ["DB_USER"],
                               password=os.environ["DB_PASSWORD"], charset="utf8mb4",
                               autocommit=True, connect_timeout=5)
    except pymysql.MySQLError as exc:
        pytest.exit(f"Cannot reach the test database: {exc}\n"
                    "Start MySQL and set TEST_DB_HOST, TEST_DB_USER and TEST_DB_PASSWORD.", returncode=2)
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{TEST_DB}` CHARACTER SET utf8mb4")
    conn.close()


# ------------------------------------------------------------------ AWS fakes
class FakeS3:
    def __init__(self):
        self.objects, self.extra_args, self.presign_calls = {}, {}, []
        self.link_override = None

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):
        self.objects[key] = fileobj.read()
        self.extra_args[key] = ExtraArgs

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.presign_calls.append((operation, Params, ExpiresIn))
        return self.link_override or f"https://files.example/{Params['Key']}?signed=1"


class FakeTable:
    """Stands in for the DynamoDB table that the Lambda checker writes to."""

    def __init__(self):
        self.items, self.denied = {}, False

    def set(self, key, status, reason=""):
        self.items[key] = {"file_key": key, "status": status, "reason": reason}

    def get_item(self, Key):
        if self.denied:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "GetItem")
        item = self.items.get(Key["file_key"])
        return {"Item": item} if item else {}


# ------------------------------------------------------------------ app and database
@pytest.fixture(scope="session")
def clouddesk():
    import app
    conn = app.connect()
    try:
        with conn.cursor() as cur:  # start from a clean schema, whatever an earlier run left behind
            cur.execute("DROP TABLE IF EXISTS comments, tickets, users")
    finally:
        conn.close()
    app.init_db()
    return app


@pytest.fixture(autouse=True)
def clean_db(clouddesk):
    clouddesk.init_db()
    conn = clouddesk.connect()
    try:
        with conn.cursor() as cur:
            for table in ("comments", "tickets", "users"):
                cur.execute(f"DELETE FROM {table}")
                cur.execute(f"ALTER TABLE {table} AUTO_INCREMENT = 1")
    finally:
        conn.close()


@pytest.fixture
def aws(clouddesk, monkeypatch):
    fakes = SimpleNamespace(s3=FakeS3(), table=FakeTable())
    monkeypatch.setattr(clouddesk, "s3", fakes.s3)
    monkeypatch.setattr(clouddesk, "ddb", fakes.table)
    return fakes


# ------------------------------------------------------------------ helpers for the tests
class Session:
    """One browser session: a Flask test client that remembers cookies."""

    def __init__(self, site, client, email=None, password=None, user_id=None, name=None):
        self.site, self.client = site, client
        self.email, self.password, self.id, self.name = email, password, user_id, name

    def get(self, url, **kwargs):
        return self.client.get(url, **kwargs)

    def text(self, url):
        return self.client.get(url).get_data(as_text=True)

    def token(self, url="/home"):
        return self.site.token(self.client, url)

    def post(self, url, data=None, **kwargs):
        data = dict(data or {})
        data.setdefault("csrf_token", self.token())
        return self.client.post(url, data=data, **kwargs)


class Site:
    def __init__(self, app):
        self.app = app

    def anonymous(self):
        return Session(self, self.app.app.test_client())

    @staticmethod
    def token(client, url):
        html = client.get(url).get_data(as_text=True)
        match = re.search(r'name="csrf_token" value="([^"]+)"', html)
        return match.group(1) if match else None

    def register(self, client, name, email, phone="+966 55 123 4567", password="password123", confirm=None):
        return client.post("/register", data=dict(
            csrf_token=self.token(client, "/register"), full_name=name, email=email, phone=phone,
            password=password, confirm=password if confirm is None else confirm))

    def sql(self, statement, args=()):
        conn = self.app.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(statement, args)
                return list(cur.fetchall())
        finally:
            conn.close()

    def make_user(self, name, email, role="requester", phone="+966 55 123 4567", password="password123"):
        client = self.app.app.test_client()
        response = self.register(client, name, email, phone, password)
        assert response.status_code == 302, "registration helper failed"
        user_id = self.sql("SELECT id FROM users WHERE email=%s", (email.lower(),))[0]["id"]
        if role != "requester":
            self.sql("UPDATE users SET role=%s WHERE id=%s", (role, user_id))
        return Session(self, client, email, password, user_id, name)

    def add_ticket(self, title, requester_id=None, status="New", priority="Moderate", category="Software",
                   assignee_id=None, attachment=None):
        conn = self.app.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO tickets (title, description, status, priority, category, requester_id, "
                            "assignee_id, attachment) VALUES (%s,'',%s,%s,%s,%s,%s,%s)",
                            (title, status, priority, category, requester_id, assignee_id, attachment))
                ticket_id = cur.lastrowid
                cur.execute("UPDATE tickets SET ticket_number=%s WHERE id=%s",
                            (f"INC{1000 + ticket_id:07d}", ticket_id))
                return ticket_id
        finally:
            conn.close()


@pytest.fixture
def site(clouddesk):
    return Site(clouddesk)


@pytest.fixture
def alice(site):
    return site.make_user("Alice Smith", "alice@example.com")


@pytest.fixture
def bob(site):
    return site.make_user("Bob Jones", "bob@example.com")


@pytest.fixture
def carol(site):
    return site.make_user("Carol Agent", "carol@example.com", role="agent")


def kpi(html, label):
    """The number shown on a dashboard tile."""
    match = re.search(r'<div class="kpi-n">(\d+)</div><div class="kpi-l">' + re.escape(label), html)
    return int(match.group(1)) if match else None
