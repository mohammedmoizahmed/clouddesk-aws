"""A database created by the first version of the app is upgraded automatically."""


def test_version_1_database_is_upgraded_in_place(clouddesk, site):
    conn = clouddesk.connect()
    try:
        with conn.cursor() as cur:
            for table in ("comments", "tickets", "users"):
                cur.execute(f"DROP TABLE IF EXISTS {table}")
            cur.execute("CREATE TABLE tickets (id INT AUTO_INCREMENT PRIMARY KEY, title VARCHAR(255) NOT NULL, "
                        "description TEXT, status VARCHAR(20) NOT NULL DEFAULT 'Open', attachment VARCHAR(512), "
                        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            cur.execute("INSERT INTO tickets (title, status) VALUES ('Old one', 'Open'), ('Old two', 'Resolved')")
    finally:
        conn.close()

    clouddesk.init_db()
    clouddesk.init_db()  # running it again must change nothing

    rows = site.sql("SELECT ticket_number, status, category, priority, requester_id FROM tickets ORDER BY id")
    assert rows == [
        dict(ticket_number="INC0001001", status="New", category="General", priority="Moderate", requester_id=None),
        dict(ticket_number="INC0001002", status="Resolved", category="General", priority="Moderate", requester_id=None),
    ]
    tables = {next(iter(r.values())) for r in site.sql("SHOW TABLES")}
    assert {"users", "tickets", "comments", "audit_log"} <= tables


def index_count(site):
    rows = site.sql("SHOW INDEX FROM tickets WHERE Column_name = 'ticket_number'")
    return len(rows)


def test_starting_the_app_again_and_again_adds_no_duplicate_indexes(clouddesk, site):
    """Regression test: every start used to add another unique index on ticket_number."""
    for _ in range(5):
        clouddesk.init_db()
    assert index_count(site) == 1


def test_upgrading_an_old_database_repeatedly_adds_only_one_index(clouddesk, site):
    conn = clouddesk.connect()
    try:
        with conn.cursor() as cur:
            for table in ("comments", "tickets", "users"):
                cur.execute(f"DROP TABLE IF EXISTS {table}")
            cur.execute("CREATE TABLE tickets (id INT AUTO_INCREMENT PRIMARY KEY, title VARCHAR(255) NOT NULL, "
                        "description TEXT, status VARCHAR(20) NOT NULL DEFAULT 'Open', attachment VARCHAR(512), "
                        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    finally:
        conn.close()
    for _ in range(4):
        clouddesk.init_db()
    assert index_count(site) == 1


def test_users_table_from_the_previous_version_gains_the_new_columns(clouddesk, site):
    conn = clouddesk.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS users")
            cur.execute("CREATE TABLE users (id INT AUTO_INCREMENT PRIMARY KEY, full_name VARCHAR(120) NOT NULL, "
                        "email VARCHAR(190) NOT NULL UNIQUE, phone VARCHAR(30) NOT NULL, "
                        "password_hash VARCHAR(255) NOT NULL, role VARCHAR(20) NOT NULL DEFAULT 'requester', "
                        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            cur.execute("INSERT INTO users (full_name, email, phone, password_hash, role) "
                        "VALUES ('Old Agent', 'old@example.com', '+966551112222', 'x', 'agent')")
    finally:
        conn.close()
    clouddesk.init_db()
    clouddesk.init_db()
    row = site.sql("SELECT role, active, must_change_password, session_version FROM users")[0]
    assert row == dict(role="agent", active=1, must_change_password=0, session_version=0)
