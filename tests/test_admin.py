"""Administrator powers: managing users, permanent deletes, editing incidents and the audit log."""
import io

import pytest

from test_incidents import new_incident

NEW_USER = dict(full_name="Dina Support", email="dina@example.com", phone="+966 55 765 4321",
                role="agent", password="Start-Pass-9", must_change="1")


def audit_actions(site):
    return [r["action"] for r in site.sql("SELECT action FROM audit_log ORDER BY id")]


class TestWhoIsAllowedIn:
    @pytest.mark.parametrize("page", ["/admin/users", "/admin/users/new", "/admin/audit"])
    def test_only_administrators_open_the_admin_pages(self, alice, carol, admin, page):
        assert alice.get(page).status_code == 403
        assert carol.get(page).status_code == 403
        assert admin.get(page).status_code == 200

    def test_admin_pages_need_a_sign_in(self, site):
        assert site.anonymous().get("/admin/users").status_code == 302

    @pytest.mark.parametrize("user_fixture", ["alice", "carol"])
    def test_other_roles_cannot_use_the_admin_actions(self, site, request, user_fixture, bob):
        user = request.getfixturevalue(user_fixture)
        site.add_ticket("Someone's incident", bob.id)
        victim = site.sql("SELECT id FROM users WHERE email='bob@example.com'")[0]["id"]
        site.sql("INSERT INTO comments (ticket_id, user_id, body) VALUES (1, %s, 'hello')", (victim,))
        attempts = [("post", "/admin/users/new", NEW_USER),
                    ("post", f"/admin/users/{victim}", dict(full_name="Bob Renamed", email="bob@example.com", phone="+966551234567", role="administrator", active="1")),
                    ("post", f"/admin/users/{victim}/password", dict(password="hacked-pass-1")),
                    ("get", f"/admin/users/{victim}/delete", None), ("post", f"/admin/users/{victim}/delete", {}),
                    ("get", "/incidents/1/edit", None), ("post", "/incidents/1/edit", dict(title="x")),
                    ("get", "/incidents/1/delete", None), ("post", "/incidents/1/delete", {}),
                    ("post", "/comments/1/delete", {})]
        for method, url, data in attempts:
            response = user.get(url) if method == "get" else user.post(url, data)
            assert response.status_code == 403, f"{user_fixture} was able to {method} {url}"
        assert site.sql("SELECT role FROM users WHERE id=%s", (victim,))[0]["role"] == "requester"
        assert site.sql("SELECT COUNT(*) AS n FROM tickets")[0]["n"] == 1
        assert site.sql("SELECT COUNT(*) AS n FROM comments")[0]["n"] == 1

    def test_menus(self, alice, carol, admin):
        assert "Administration" in admin.text("/home") and "Audit log" in admin.text("/home")
        assert "Administration" not in carol.text("/home") and "Administration" not in alice.text("/home")


class TestCreatingUsers:
    def test_admin_creates_a_user_who_can_then_sign_in(self, site, admin):
        response = admin.post("/admin/users/new", NEW_USER)
        assert response.status_code == 302 and response.location.endswith("/admin/users")
        row = site.sql("SELECT role, active, must_change_password, password_hash FROM users WHERE email='dina@example.com'")[0]
        assert row["role"] == "agent" and row["active"] == 1 and row["must_change_password"] == 1
        assert "Start-Pass-9" not in row["password_hash"]
        signed_in = site.login(site.anonymous().client, "dina@example.com", "Start-Pass-9")
        assert signed_in.status_code == 302 and signed_in.location.endswith("/home")

    def test_new_user_must_choose_their_own_password_when_asked(self, site, admin):
        admin.post("/admin/users/new", NEW_USER)
        client = site.anonymous()
        site.login(client.client, "dina@example.com", "Start-Pass-9")
        assert client.get("/home").location.endswith("/profile")

    def test_the_forced_change_can_be_switched_off(self, site, admin):
        admin.post("/admin/users/new", dict(NEW_USER, must_change=""))
        assert site.sql("SELECT must_change_password AS m FROM users WHERE email='dina@example.com'")[0]["m"] == 0

    @pytest.mark.parametrize("override, message", [
        (dict(full_name="Dina"), "full name"),
        (dict(email="not-an-email"), "valid email"),
        (dict(phone="abc"), "phone"),
        (dict(password="short"), "at least 8"),
        (dict(role="superuser"), "Choose a role"),
    ])
    def test_validation(self, site, admin, override, message):
        response = admin.post("/admin/users/new", dict(NEW_USER, **override))
        assert response.status_code == 200 and message in response.get_data(as_text=True)
        assert site.sql("SELECT COUNT(*) AS n FROM users")[0]["n"] == 1  # only the admin

    def test_duplicate_email_is_refused(self, site, admin, alice):
        response = admin.post("/admin/users/new", dict(NEW_USER, email="ALICE@example.com"))
        assert "already exists" in response.get_data(as_text=True)

    def test_the_form_does_not_echo_the_password_back(self, admin):
        response = admin.post("/admin/users/new", dict(NEW_USER, email="not-an-email"))
        assert "Start-Pass-9" not in response.get_data(as_text=True)

    def test_creation_is_audited(self, site, admin):
        admin.post("/admin/users/new", NEW_USER)
        assert audit_actions(site) == ["user.create"]
        assert "Start-Pass-9" not in str(site.sql("SELECT detail FROM audit_log"))

    def test_all_three_roles_can_be_created(self, site, admin):
        for i, role in enumerate(["requester", "agent", "administrator"]):
            admin.post("/admin/users/new", dict(NEW_USER, email=f"user{i}@example.com", role=role))
        roles = [r["role"] for r in site.sql("SELECT role FROM users WHERE email LIKE 'user%'")]
        assert sorted(roles) == ["administrator", "agent", "requester"]


class TestEditingUsers:
    def test_changing_a_role_takes_effect_immediately(self, site, admin, alice, bob):
        site.add_ticket("Bob's incident", bob.id)
        assert "Bob's incident" not in alice.text("/incidents?scope=all")
        admin.post(f"/admin/users/{alice.id}", dict(full_name="Alice Smith", email=alice.email,
                                                    phone="+966 55 123 4567", role="agent", active="1"))
        assert "Bob&#39;s incident" in alice.text("/incidents?scope=all")

    def test_name_email_and_phone_can_be_edited(self, site, admin, alice):
        admin.post(f"/admin/users/{alice.id}", dict(full_name="Alice Renamed", email="new@example.com",
                                                    phone="0551112222", role="requester", active="1"))
        row = site.sql("SELECT full_name, email, phone FROM users WHERE id=%s", (alice.id,))[0]
        assert row == dict(full_name="Alice Renamed", email="new@example.com", phone="0551112222")

    def test_email_must_stay_unique(self, site, admin, alice, bob):
        response = admin.post(f"/admin/users/{alice.id}", dict(
            full_name="Alice Smith", email=bob.email, phone="+966 55 123 4567", role="requester", active="1"))
        assert "already has this email" in response.get_data(as_text=True)

    def test_invalid_role_is_refused(self, site, admin, alice):
        response = admin.post(f"/admin/users/{alice.id}", dict(
            full_name="Alice Smith", email=alice.email, phone="+966 55 123 4567", role="root", active="1"))
        assert "Choose a role" in response.get_data(as_text=True)
        assert site.sql("SELECT role FROM users WHERE id=%s", (alice.id,))[0]["role"] == "requester"

    def test_disabling_cuts_off_a_signed_in_user_and_enabling_restores_access(self, site, admin, alice):
        form = dict(full_name="Alice Smith", email=alice.email, phone="+966 55 123 4567", role="requester")
        admin.post(f"/admin/users/{alice.id}", form)  # the box is unticked: the account is disabled
        assert alice.get("/home").status_code == 302
        assert "disabled" in site.login(site.anonymous().client, alice.email).get_data(as_text=True)
        admin.post(f"/admin/users/{alice.id}", dict(form, active="1"))
        assert site.login(site.anonymous().client, alice.email).status_code == 302

    def test_an_administrator_cannot_demote_or_disable_themselves(self, site, admin):
        admin.post(f"/admin/users/{admin.id}", dict(full_name="Ada Admin", email=admin.email,
                                                    phone="+966 55 123 4567", role="requester"))
        row = site.sql("SELECT role, active FROM users WHERE id=%s", (admin.id,))[0]
        assert row == dict(role="administrator", active=1)
        assert admin.get("/admin/users").status_code == 200

    def test_one_administrator_can_demote_another(self, site, admin):
        other = site.make_user("Omar Other", "omar@example.com", role="administrator")
        admin.post(f"/admin/users/{other.id}", dict(full_name="Omar Other", email=other.email,
                                                    phone="+966 55 123 4567", role="agent", active="1"))
        assert site.sql("SELECT role FROM users WHERE id=%s", (other.id,))[0]["role"] == "agent"
        assert other.get("/admin/users").status_code == 403

    def test_edits_are_audited_with_the_old_and_new_role(self, site, admin, alice):
        admin.post(f"/admin/users/{alice.id}", dict(full_name="Alice Smith", email=alice.email,
                                                    phone="+966 55 123 4567", role="agent", active="1"))
        detail = site.sql("SELECT detail FROM audit_log")[0]["detail"]
        assert "role: requester -> agent" in detail

    def test_unknown_user_is_a_404(self, admin):
        assert admin.get("/admin/users/9999").status_code == 404

    def test_user_list_shows_everyone(self, admin, alice, carol):
        html = admin.text("/admin/users")
        assert all(name in html for name in ("Ada Admin", "Alice Smith", "Carol Agent"))
        assert "(you)" in html and "Administrator" in html


class TestPasswords:
    def test_admin_sets_a_password_and_the_old_one_stops_working(self, site, admin, alice):
        admin.post(f"/admin/users/{alice.id}/password", dict(password="Brand-New-7", must_change="1"))
        assert site.login(site.anonymous().client, alice.email, "password123").status_code == 200
        assert site.login(site.anonymous().client, alice.email, "Brand-New-7").status_code == 302

    def test_it_signs_the_person_out_everywhere(self, site, admin, alice):
        assert alice.get("/home").status_code == 200
        admin.post(f"/admin/users/{alice.id}/password", dict(password="Brand-New-7"))
        assert alice.get("/home").status_code == 302

    def test_forced_change_flag_follows_the_checkbox(self, site, admin, alice):
        admin.post(f"/admin/users/{alice.id}/password", dict(password="Brand-New-7", must_change="1"))
        assert site.sql("SELECT must_change_password AS m FROM users WHERE id=%s", (alice.id,))[0]["m"] == 1
        admin.post(f"/admin/users/{alice.id}/password", dict(password="Brand-New-8"))
        assert site.sql("SELECT must_change_password AS m FROM users WHERE id=%s", (alice.id,))[0]["m"] == 0

    def test_short_password_is_refused(self, site, admin, alice):
        admin.post(f"/admin/users/{alice.id}/password", dict(password="short"))
        assert site.login(site.anonymous().client, alice.email, "password123").status_code == 302

    def test_resetting_your_own_password_keeps_you_signed_in(self, admin):
        admin.post(f"/admin/users/{admin.id}/password", dict(password="Brand-New-7"))
        assert admin.get("/admin/users").status_code == 200

    def test_reset_is_audited_without_the_password(self, site, admin, alice):
        admin.post(f"/admin/users/{alice.id}/password", dict(password="Brand-New-7"))
        assert audit_actions(site) == ["user.password_reset"]
        assert "Brand-New-7" not in str(site.sql("SELECT detail FROM audit_log"))


class TestDeletingUsers:
    def test_the_confirmation_page_deletes_nothing(self, site, admin, alice):
        site.add_ticket("Alice's incident", alice.id)
        html = admin.text(f"/admin/users/{alice.id}/delete")
        assert "permanent" in html and "alice@example.com" in html and "<strong>1</strong> incident" in html
        assert site.sql("SELECT COUNT(*) AS n FROM users WHERE id=%s", (alice.id,))[0]["n"] == 1

    def test_deleted_user_is_gone_and_cannot_sign_in(self, site, admin, alice):
        admin.post(f"/admin/users/{alice.id}/delete", {})
        assert site.sql("SELECT COUNT(*) AS n FROM users WHERE id=%s", (alice.id,))[0]["n"] == 0
        assert site.login(site.anonymous().client, alice.email).status_code == 200
        assert alice.get("/home").status_code == 302

    def test_their_incidents_stay_unless_you_also_delete_them(self, site, admin, alice, carol):
        site.add_ticket("Alice's incident", alice.id)
        site.add_ticket("Assigned to Alice", None, assignee_id=alice.id)
        admin.post(f"/admin/users/{alice.id}/delete", {})
        assert site.sql("SELECT COUNT(*) AS n FROM tickets")[0]["n"] == 2
        assert site.sql("SELECT assignee_id FROM tickets WHERE id=2")[0]["assignee_id"] is None
        assert admin.get("/incidents?scope=all").status_code == 200
        assert admin.get("/incidents/1").status_code == 200

    def test_comments_of_a_deleted_user_remain_and_are_labelled(self, site, admin, alice):
        site.add_ticket("Alice's incident", alice.id)
        alice.post("/incidents/1/comment", dict(body="A comment I wrote"))
        admin.post(f"/admin/users/{alice.id}/delete", {})
        html = admin.text("/incidents/1")
        assert "A comment I wrote" in html and "Deleted user" in html

    def test_alternatively_their_incidents_can_be_deleted_with_everything_attached(self, site, admin, alice, bob, aws):
        site.add_ticket("Alice's incident", alice.id, attachment="ticket-1/shot.png")
        site.add_ticket("Bob's incident", bob.id)
        aws.s3.objects["ticket-1/shot.png"] = b"x"
        aws.table.set("ticket-1/shot.png", "accepted")
        alice.post("/incidents/1/comment", dict(body="hello"))
        admin.post(f"/admin/users/{alice.id}/delete", dict(delete_incidents="1"))
        assert [r["title"] for r in site.sql("SELECT title FROM tickets")] == ["Bob's incident"]
        assert site.sql("SELECT COUNT(*) AS n FROM comments WHERE ticket_id=1")[0]["n"] == 0
        assert "ticket-1/shot.png" not in aws.s3.objects and "ticket-1/shot.png" not in aws.table.items

    def test_an_administrator_cannot_delete_themselves(self, site, admin):
        response = admin.post(f"/admin/users/{admin.id}/delete", {}, follow_redirects=True)
        assert "cannot delete your own account" in response.get_data(as_text=True)
        assert site.sql("SELECT COUNT(*) AS n FROM users WHERE id=%s", (admin.id,))[0]["n"] == 1

    def test_the_edit_page_does_not_offer_self_deletion(self, admin, alice):
        assert "Delete user" not in admin.text(f"/admin/users/{admin.id}")
        assert "Delete user" in admin.text(f"/admin/users/{alice.id}")

    def test_deletion_is_audited(self, site, admin, alice):
        admin.post(f"/admin/users/{alice.id}/delete", {})
        assert audit_actions(site) == ["user.delete"]


class TestDeletingIncidents:
    @pytest.fixture
    def incident(self, site, alice, aws):
        site.add_ticket("Printer chip error", alice.id, attachment="ticket-1/shot.png")
        aws.s3.objects["ticket-1/shot.png"] = b"image"
        aws.table.set("ticket-1/shot.png", "accepted")
        alice.post("/incidents/1/comment", dict(body="Still broken"))

    def test_the_confirmation_page_shows_what_will_go_and_deletes_nothing(self, site, admin, incident):
        html = admin.text("/incidents/1/delete")
        assert "permanent" in html and "shot.png" in html and "<strong>1</strong> comment" in html
        assert site.sql("SELECT COUNT(*) AS n FROM tickets")[0]["n"] == 1

    def test_deleting_removes_the_incident_its_comments_and_its_file(self, site, admin, alice, incident, aws):
        response = admin.post("/incidents/1/delete", {})
        assert response.status_code == 302
        assert site.sql("SELECT COUNT(*) AS n FROM tickets")[0]["n"] == 0
        assert site.sql("SELECT COUNT(*) AS n FROM comments")[0]["n"] == 0
        assert "ticket-1/shot.png" not in aws.s3.objects and "ticket-1/shot.png" not in aws.table.items
        assert admin.get("/incidents/1").status_code == 404
        assert alice.get("/incidents/1").status_code == 404

    def test_an_incident_without_an_attachment_can_be_deleted(self, site, admin, alice):
        site.add_ticket("Plain incident", alice.id)
        admin.post("/incidents/1/delete", {})
        assert site.sql("SELECT COUNT(*) AS n FROM tickets")[0]["n"] == 0

    def test_the_incident_is_still_deleted_if_storage_refuses_but_you_are_told(self, site, admin, incident, aws):
        aws.s3.deny_delete = True
        response = admin.post("/incidents/1/delete", {}, follow_redirects=True)
        assert site.sql("SELECT COUNT(*) AS n FROM tickets")[0]["n"] == 0
        assert "could not be fully removed" in response.get_data(as_text=True)

    def test_neither_agents_nor_the_owner_can_delete(self, site, alice, carol, incident):
        assert carol.post("/incidents/1/delete", {}).status_code == 403
        assert alice.post("/incidents/1/delete", {}).status_code == 403
        assert site.sql("SELECT COUNT(*) AS n FROM tickets")[0]["n"] == 1

    def test_the_next_incident_does_not_reuse_the_number(self, site, admin, alice, incident):
        admin.post("/incidents/1/delete", {})
        new_incident(alice)
        assert site.sql("SELECT ticket_number FROM tickets")[0]["ticket_number"] == "INC0001002"

    def test_deletion_is_audited_with_the_number(self, site, admin, incident):
        admin.post("/incidents/1/delete", {})
        assert audit_actions(site) == ["incident.delete"]
        assert "INC0001001" in site.sql("SELECT detail FROM audit_log")[0]["detail"]

    def test_unknown_incident_is_a_404(self, admin):
        assert admin.get("/incidents/999/delete").status_code == 404


class TestEditingIncidents:
    FORM = dict(title="Edited title", description="Edited text", category="Network", priority="Critical", status="On Hold")

    def test_administrator_can_change_everything(self, site, admin, alice, carol):
        site.add_ticket("Original", alice.id)
        response = admin.post("/incidents/1/edit", dict(self.FORM, requester=str(alice.id), assignee=str(carol.id)))
        assert response.status_code == 302
        row = site.sql("SELECT title, description, category, priority, status, assignee_id FROM tickets")[0]
        assert row == dict(title="Edited title", description="Edited text", category="Network", priority="Critical",
                           status="On Hold", assignee_id=carol.id)

    def test_the_caller_can_be_changed(self, site, admin, alice, bob):
        site.add_ticket("Original", alice.id)
        admin.post("/incidents/1/edit", dict(self.FORM, requester=str(bob.id), assignee=""))
        assert site.sql("SELECT requester_id FROM tickets")[0]["requester_id"] == bob.id
        assert bob.get("/incidents/1").status_code == 200 and alice.get("/incidents/1").status_code == 404

    def test_changes_appear_in_the_activity_stream_and_the_audit_log(self, site, admin, alice):
        site.add_ticket("Original", alice.id)
        admin.post("/incidents/1/edit", dict(self.FORM, requester=str(alice.id), assignee=""))
        assert "Edited by an administrator: short description" in alice.text("/incidents/1")
        assert audit_actions(site) == ["incident.edit"]

    def test_an_unchanged_form_records_nothing(self, site, admin, alice):
        site.add_ticket("Original", alice.id, priority="Moderate", category="Software")
        admin.post("/incidents/1/edit", dict(title="Original", description="", category="Software",
                                             priority="Moderate", status="New", requester=str(alice.id), assignee=""))
        assert audit_actions(site) == []

    @pytest.mark.parametrize("override, message", [
        (dict(title=" "), "Enter a short description"), (dict(priority="Urgent"), "Choose a priority"),
        (dict(status="Done"), "Choose a state"), (dict(category="Magic"), "Choose a category"),
        (dict(requester="9999"), "Choose a caller"), (dict(assignee="9999"), "Choose an assignee"),
    ])
    def test_validation(self, site, admin, alice, override, message):
        site.add_ticket("Original", alice.id)
        response = admin.post("/incidents/1/edit", {**self.FORM, "requester": str(alice.id), "assignee": "", **override})
        assert message in response.get_data(as_text=True)
        assert site.sql("SELECT title FROM tickets")[0]["title"] == "Original"

    def test_a_requester_cannot_be_assigned_the_incident(self, site, admin, alice, bob):
        site.add_ticket("Original", alice.id)
        response = admin.post("/incidents/1/edit", dict(self.FORM, requester=str(alice.id), assignee=str(bob.id)))
        assert "Choose an assignee" in response.get_data(as_text=True)

    def test_the_edit_page_renders_with_current_values(self, site, admin, alice):
        site.add_ticket("Original title", alice.id)
        assert "Original title" in admin.text("/incidents/1/edit")

    def test_the_record_page_shows_edit_and_delete_to_administrators_only(self, site, admin, carol, alice):
        site.add_ticket("Original", alice.id)
        assert "/incidents/1/edit" in admin.text("/incidents/1") and "/incidents/1/delete" in admin.text("/incidents/1")
        assert "/incidents/1/edit" not in carol.text("/incidents/1") and "/incidents/1/delete" not in carol.text("/incidents/1")


class TestComments:
    def test_administrator_can_delete_a_comment(self, site, admin, alice):
        site.add_ticket("Original", alice.id)
        alice.post("/incidents/1/comment", dict(body="Please remove me"))
        comment_id = site.sql("SELECT id FROM comments")[0]["id"]
        admin.post(f"/comments/{comment_id}/delete", {})
        assert "Please remove me" not in admin.text("/incidents/1")
        assert audit_actions(site) == ["comment.delete"]

    def test_unknown_comment_is_a_404(self, admin):
        assert admin.post("/comments/999/delete", {}).status_code == 404


class TestAdministratorsWorkIncidentsToo:
    def test_an_administrator_sees_every_incident(self, site, admin, alice, bob):
        site.add_ticket("Alice laptop", alice.id)
        site.add_ticket("Bob printer", bob.id)
        html = admin.text("/incidents?scope=all")
        assert "Alice laptop" in html and "Bob printer" in html

    def test_an_administrator_can_work_an_incident_like_an_agent(self, site, admin, alice):
        site.add_ticket("Alice laptop", alice.id)
        admin.post("/incidents/1/update", dict(state="In Progress", priority="High", assignee=str(admin.id)))
        assert site.sql("SELECT status, assignee_id FROM tickets")[0] == dict(status="In Progress", assignee_id=admin.id)

    def test_administrators_appear_in_the_assignee_list(self, site, admin, carol, alice):
        site.add_ticket("Alice laptop", alice.id)
        assert "Ada Admin" in carol.text("/incidents/1")

    def test_their_comments_are_marked_as_administrator(self, site, admin, alice):
        site.add_ticket("Alice laptop", alice.id)
        admin.post("/incidents/1/comment", dict(body="On it"))
        assert "Administrator" in alice.text("/incidents/1")

    def test_the_dashboard_counts_everything(self, site, admin, alice, bob):
        from conftest import kpi
        site.add_ticket("A", alice.id)
        site.add_ticket("B", bob.id)
        assert kpi(admin.text("/home"), "Total incidents") == 2


class TestAuditLog:
    def test_it_lists_actions_with_who_did_them(self, site, admin, alice):
        admin.post("/admin/users/new", NEW_USER)
        site.add_ticket("Alice laptop", alice.id)
        admin.post("/incidents/1/delete", {})
        html = admin.text("/admin/audit")
        assert "user.create" in html and "incident.delete" in html and "Ada Admin" in html

    def test_it_survives_the_deletion_of_the_person_who_acted(self, site, admin):
        other = site.make_user("Omar Other", "omar@example.com", role="administrator")
        other.post("/admin/users/new", NEW_USER)
        admin.post(f"/admin/users/{other.id}/delete", {})
        assert "Omar Other" in admin.text("/admin/audit")

    def test_empty_message(self, admin):
        assert "Nothing has been recorded yet" in admin.text("/admin/audit")
