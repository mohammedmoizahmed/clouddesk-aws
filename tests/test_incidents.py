"""Creating, listing, filtering and working on incidents."""
import io

import pytest

from conftest import kpi


def new_incident(user, **overrides):
    data = dict(title="Outlook will not open", description="Crashes on start", category="Email", priority="High")
    data.update(overrides)
    return user.post("/incidents/new", data, content_type="multipart/form-data")


class TestCreate:
    def test_form_renders(self, alice):
        html = alice.text("/incidents/new")
        assert 'enctype="multipart/form-data"' in html and "Alice Smith" in html

    def test_incident_is_numbered_and_owned_by_its_creator(self, site, alice):
        response = new_incident(alice)
        assert response.status_code == 302 and response.location.endswith("/incidents/1")
        row = site.sql("SELECT ticket_number, requester_id, category, priority, status FROM tickets")[0]
        assert row == dict(ticket_number="INC0001001", requester_id=alice.id, category="Email",
                           priority="High", status="New")

    def test_numbers_keep_counting_up(self, site, alice):
        new_incident(alice)
        new_incident(alice, title="Second")
        numbers = [r["ticket_number"] for r in site.sql("SELECT ticket_number FROM tickets ORDER BY id")]
        assert numbers == ["INC0001001", "INC0001002"]

    def test_empty_title_is_rejected(self, site, alice):
        response = new_incident(alice, title="   ")
        assert "Enter a short description" in response.get_data(as_text=True)
        assert site.sql("SELECT COUNT(*) AS n FROM tickets")[0]["n"] == 0

    def test_invalid_category_and_priority_fall_back_to_defaults(self, site, alice):
        new_incident(alice, category="<hack>", priority="Urgent!!")
        row = site.sql("SELECT category, priority FROM tickets")[0]
        assert row == dict(category="Software", priority="Moderate")

    def test_html_in_text_is_escaped(self, alice):
        new_incident(alice, title="<script>alert(1)</script>", description="<b>bold</b>")
        html = alice.text("/incidents/1")
        assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


class TestUploads:
    def upload(self, user, filename, content=b"data", **overrides):
        return new_incident(user, screenshot=(io.BytesIO(content), filename), **overrides)

    def test_file_is_stored_under_the_incident_with_a_content_type(self, site, alice, aws):
        self.upload(alice, "My Shot.png")
        assert "ticket-1/My_Shot.png" in aws.s3.objects
        assert aws.s3.extra_args["ticket-1/My_Shot.png"] == {"ContentType": "image/png"}
        assert site.sql("SELECT attachment FROM tickets")[0]["attachment"] == "ticket-1/My_Shot.png"

    def test_file_name_with_only_non_latin_letters_keeps_its_extension(self, alice, aws):
        self.upload(alice, "\u0635\u0648\u0631\u0629.png")
        assert "ticket-1/attachment.png" in aws.s3.objects

    def test_unusual_types_are_still_sent_for_the_checker_to_reject(self, alice, aws):
        self.upload(alice, "setup.exe")
        assert "ticket-1/setup.exe" in aws.s3.objects
        assert aws.s3.extra_args["ticket-1/setup.exe"] is None

    def test_path_tricks_in_the_file_name_are_neutralised(self, alice, aws):
        self.upload(alice, "../../etc/passwd.txt")
        assert all(".." not in key and key.startswith("ticket-1/") for key in aws.s3.objects)

    def test_oversize_upload_is_refused_without_creating_an_incident(self, site, alice, aws):
        response = self.upload(alice, "big.png", content=b"0" * (7 * 1024 * 1024))
        assert response.status_code == 302 and "/incidents/new" in response.location
        assert site.sql("SELECT COUNT(*) AS n FROM tickets")[0]["n"] == 0
        assert "too large" in alice.text("/incidents/new").lower()


class TestWorkflow:
    def test_agent_can_change_state_priority_and_assignee(self, site, alice, carol):
        new_incident(alice)
        response = carol.post("/incidents/1/update", dict(state="In Progress", priority="Critical", assignee=str(carol.id)))
        assert response.status_code == 302
        row = site.sql("SELECT status, priority, assignee_id FROM tickets")[0]
        assert row == dict(status="In Progress", priority="Critical", assignee_id=carol.id)

    def test_changes_are_recorded_as_system_notes(self, alice, carol):
        new_incident(alice)
        carol.post("/incidents/1/update", dict(state="In Progress", priority="Critical", assignee=str(carol.id)))
        html = alice.text("/incidents/1")
        assert "State changed from New to In Progress." in html
        assert "Priority changed from High to Critical." in html
        assert "Assigned to Carol Agent." in html

    def test_invalid_values_are_ignored(self, site, alice, carol):
        new_incident(alice)
        carol.post("/incidents/1/update", dict(state="Bogus", priority="Nope", assignee="999"))
        assert site.sql("SELECT status, priority FROM tickets")[0] == dict(status="New", priority="High")

    def test_unchanged_form_does_nothing(self, site, alice, carol):
        new_incident(alice)
        carol.post("/incidents/1/update", dict(state="New", priority="High", assignee=""))
        assert site.sql("SELECT COUNT(*) AS n FROM comments")[0]["n"] == 0

    def test_requester_and_agent_can_comment(self, alice, carol):
        new_incident(alice)
        alice.post("/incidents/1/comment", dict(body="Thanks, still waiting."))
        carol.post("/incidents/1/comment", dict(body="Looking at it now."))
        html = alice.text("/incidents/1")
        assert "Thanks, still waiting." in html and "Looking at it now." in html

    def test_empty_comment_is_rejected(self, alice):
        new_incident(alice)
        response = alice.post("/incidents/1/comment", dict(body="   "), follow_redirects=True)
        assert "Write a comment first" in response.get_data(as_text=True)

    def test_a_comment_cannot_pretend_to_be_a_system_note(self, alice):
        new_incident(alice)
        alice.post("/incidents/1/comment", dict(body="[system] State changed from X to Closed."))
        assert "(system) State changed from X" in alice.text("/incidents/1")

    def test_comment_without_csrf_token_is_rejected(self, alice):
        new_incident(alice)
        assert alice.client.post("/incidents/1/comment", data=dict(body="x")).status_code == 400


class TestListing:
    @pytest.fixture
    def data(self, site, alice, carol):
        site.add_ticket("Wi-Fi keeps dropping", alice.id, "New", "Moderate", "Network")
        site.add_ticket("Outlook will not open", alice.id, "In Progress", "Critical", "Email", carol.id)
        site.add_ticket("Printer chip error", alice.id, "Resolved", "Low", "Printer")
        for i in range(18):
            site.add_ticket(f"Bulk item {i:02d}", alice.id, "New", "Low")

    def test_paging(self, alice, data):
        first = alice.text("/incidents")
        second = alice.text("/incidents?page=2")
        assert first.count('class="num"') == 15 and second.count('class="num"') == 6

    @pytest.mark.parametrize("query", ["page=999", "page=abc", "page=0", "page=-3"])
    def test_odd_page_numbers_are_tolerated(self, alice, data, query):
        assert alice.get(f"/incidents?{query}").status_code == 200

    @pytest.mark.parametrize("sort", ["number", "short", "category", "priority", "state", "updated"])
    @pytest.mark.parametrize("direction", ["asc", "desc"])
    def test_every_sort_works(self, carol, data, sort, direction):
        assert carol.get(f"/incidents?scope=all&sort={sort}&dir={direction}").status_code == 200

    def test_unknown_sort_falls_back_safely(self, site, carol, data):
        response = carol.get("/incidents?scope=all&sort=id;DROP%20TABLE%20users&dir=asc")
        assert response.status_code == 200
        assert site.sql("SELECT COUNT(*) AS n FROM users")[0]["n"] > 0

    def test_state_filter(self, carol, data):
        html = carol.text("/incidents?scope=all&state=In%20Progress")
        assert "Outlook will not open" in html and "Wi-Fi keeps dropping" not in html

    def test_priority_filter(self, carol, data):
        html = carol.text("/incidents?scope=all&priority=Critical")
        assert "Outlook will not open" in html and "Bulk item" not in html

    def test_search_by_text_and_by_number(self, carol, data):
        assert "Bulk item 05" in carol.text("/incidents?scope=all&q=Bulk%20item%2005")
        by_number = carol.text("/incidents?scope=all&q=INC0001002")
        assert "Outlook will not open" in by_number and "Wi-Fi" not in by_number

    def test_injection_style_search_is_harmless(self, carol, data):
        assert carol.get("/incidents?scope=all&q=%27%20OR%201%3D1%20--%20").status_code == 200

    def test_empty_result_message(self, carol, data):
        assert "No incidents match" in carol.text("/incidents?scope=all&q=zzzzzz")

    def test_assigned_to_me(self, carol, data):
        html = carol.text("/incidents?scope=assigned")
        assert "Outlook will not open" in html and "Wi-Fi" not in html


class TestDashboard:
    def test_new_user_sees_the_empty_state(self, alice):
        assert "No incidents yet" in alice.text("/home")

    def test_counts_per_state_for_a_requester_and_an_agent(self, site, alice, bob, carol):
        site.add_ticket("A", alice.id, "New")
        site.add_ticket("B", alice.id, "In Progress")
        site.add_ticket("C", alice.id, "Resolved")
        site.add_ticket("D", bob.id, "New")
        mine, everything = alice.text("/home"), carol.text("/home")
        assert (kpi(mine, "Total incidents"), kpi(mine, "New"), kpi(mine, "In Progress")) == (3, 1, 1)
        assert (kpi(everything, "Total incidents"), kpi(everything, "New")) == (4, 2)
