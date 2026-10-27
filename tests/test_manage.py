"""The command-line tool used for first-time setup and emergencies."""
import pytest

import manage


def test_create_user_then_sign_in(site):
    manage.create_user("First Admin", "first@example.com", "+966 55 111 2222", "administrator", "Start-Pass-1")
    client = site.anonymous()
    response = site.login(client.client, "first@example.com", "Start-Pass-1")
    assert response.status_code == 302
    assert client.get("/admin/users").status_code == 200


@pytest.mark.parametrize("args, message", [
    (("Solo", "a@example.com", "+966551112222", "agent", "password123"), "full name"),
    (("Full Name", "bad", "+966551112222", "agent", "password123"), "valid email"),
    (("Full Name", "a@example.com", "x", "agent", "password123"), "phone"),
    (("Full Name", "a@example.com", "+966551112222", "root", "password123"), "role"),
    (("Full Name", "a@example.com", "+966551112222", "agent", "short"), "8 characters"),
])
def test_create_user_validation(site, args, message):
    with pytest.raises(ValueError, match=message):
        manage.create_user(*args)
    assert site.sql("SELECT COUNT(*) AS n FROM users")[0]["n"] == 0


def test_duplicate_email(site, alice):
    with pytest.raises(ValueError, match="already exists"):
        manage.create_user("Alice Again", "ALICE@example.com", "+966551112222", "agent", "password123")


def test_set_role_promotes_an_existing_account(site, alice):
    manage.set_role("Alice@Example.com", "administrator")
    assert alice.get("/admin/users").status_code == 200


def test_set_role_errors(site):
    with pytest.raises(ValueError, match="No user"):
        manage.set_role("nobody@example.com", "agent")
    with pytest.raises(ValueError, match="role"):
        manage.set_role("nobody@example.com", "root")


def test_set_password_signs_everyone_out_and_reenables_the_account(site, alice):
    site.sql("UPDATE users SET active=0 WHERE id=%s", (alice.id,))
    manage.set_password(alice.email, "Recovered-1")
    assert alice.get("/home").status_code == 302
    assert site.login(site.anonymous().client, alice.email, "Recovered-1").status_code == 302


def test_set_password_errors(site):
    with pytest.raises(ValueError, match="No user"):
        manage.set_password("nobody@example.com", "password123")
    with pytest.raises(ValueError, match="8 characters"):
        manage.set_password("nobody@example.com", "short")


def test_list_users(site, alice, carol):
    assert [u["email"] for u in manage.list_users()] == ["alice@example.com", "carol@example.com"]
