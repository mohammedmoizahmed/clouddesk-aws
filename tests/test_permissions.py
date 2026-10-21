"""Who can see and do what."""
from test_incidents import new_incident


def test_requester_sees_only_their_own_incidents(site, alice, bob):
    site.add_ticket("Alice laptop problem", alice.id)
    site.add_ticket("Bob printer problem", bob.id)
    site.add_ticket("Old unowned ticket", None)
    html = alice.text("/incidents")
    assert "Alice laptop problem" in html
    assert "Bob printer problem" not in html and "Old unowned ticket" not in html


def test_requester_cannot_widen_the_list_with_scope(site, alice, bob):
    site.add_ticket("Bob printer problem", bob.id)
    assert "Bob printer problem" not in alice.text("/incidents?scope=all")


def test_someone_elses_incident_looks_like_it_does_not_exist(site, alice, bob):
    site.add_ticket("Alice laptop problem", alice.id)
    assert bob.get("/incidents/1").status_code == 404


def test_unowned_old_incident_is_hidden_from_requesters(site, alice):
    site.add_ticket("Old unowned ticket", None)
    assert alice.get("/incidents/1").status_code == 404


def test_cannot_comment_on_someone_elses_incident(site, alice, bob):
    site.add_ticket("Alice laptop problem", alice.id)
    assert bob.post("/incidents/1/comment", dict(body="hi")).status_code == 404


def test_requester_cannot_change_state_even_on_their_own_incident(site, alice):
    site.add_ticket("Alice laptop problem", alice.id)
    assert alice.post("/incidents/1/update", dict(state="Closed")).status_code == 403
    assert site.sql("SELECT status FROM tickets")[0]["status"] == "New"


def test_agent_sees_everything_including_old_unowned_incidents(site, alice, bob, carol):
    site.add_ticket("Alice laptop problem", alice.id)
    site.add_ticket("Bob printer problem", bob.id)
    site.add_ticket("Old unowned ticket", None)
    html = carol.text("/incidents?scope=all")
    assert all(name in html for name in ("Alice laptop problem", "Bob printer problem", "Old unowned ticket"))


def test_agent_sees_caller_details_and_the_update_panel_but_requesters_do_not(site, alice, carol):
    site.add_ticket("Alice laptop problem", alice.id)
    agent_view, requester_view = carol.text("/incidents/1"), alice.text("/incidents/1")
    assert "<dt>Email</dt>" in agent_view and "<dt>Phone</dt>" in agent_view and "Work on this incident" in agent_view
    assert "<dt>Email</dt>" not in requester_view and "<dt>Phone</dt>" not in requester_view
    assert "Work on this incident" not in requester_view


def test_menus_differ_by_role(alice, carol):
    assert "All incidents" not in alice.text("/home") and "Assigned to me" not in alice.text("/home")
    assert "All incidents" in carol.text("/home") and "Assigned to me" in carol.text("/home")


def test_agent_gets_404_for_an_incident_that_does_not_exist(carol):
    assert carol.get("/incidents/999").status_code == 404


def test_role_is_read_from_the_database_on_every_request(site, alice):
    new_incident(alice)
    assert alice.post("/incidents/1/update", dict(state="Closed")).status_code == 403
    site.sql("UPDATE users SET role='agent' WHERE id=%s", (alice.id,))
    assert alice.post("/incidents/1/update", dict(state="Closed")).status_code == 302


def test_deleted_account_is_signed_out(site, alice):
    site.sql("DELETE FROM users WHERE id=%s", (alice.id,))
    assert alice.get("/home").status_code == 302
