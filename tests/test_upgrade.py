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
    assert {"users", "tickets", "comments"} <= tables


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
