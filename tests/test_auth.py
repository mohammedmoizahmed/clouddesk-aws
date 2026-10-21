"""Registration, sign-in, sessions and request protection."""
import pytest


def test_health_page_is_open_and_needs_no_login(site):
    response = site.anonymous().get("/health")
    assert response.status_code == 200 and response.get_data(as_text=True) == "OK"


def test_root_redirects_to_sign_in(site):
    response = site.anonymous().get("/")
    assert response.status_code == 302 and "/login" in response.location


def test_protected_page_redirects_and_remembers_where_you_were_going(site):
    response = site.anonymous().get("/incidents?scope=all")
    assert response.status_code == 302 and "next=" in response.location


@pytest.mark.parametrize("path", ["/login", "/register"])
def test_public_pages_render(site, path):
    assert site.anonymous().get(path).status_code == 200


def test_post_without_csrf_token_is_rejected(site):
    visitor = site.anonymous()
    response = visitor.client.post("/login", data=dict(email="a@b.co", password="x"))
    assert response.status_code == 400


@pytest.mark.parametrize("fields, message", [
    (dict(name="Alice", email="alice@example.com"), "full name"),
    (dict(name="Alice Smith", email="not-an-email"), "valid email"),
    (dict(name="Alice Smith", email="alice@example.com", phone="abc"), "phone"),
    (dict(name="Alice Smith", email="alice@example.com", password="short"), "at least 8"),
    (dict(name="Alice Smith", email="alice@example.com", confirm="different1"), "do not match"),
])
def test_registration_validation(site, fields, message):
    visitor = site.anonymous()
    response = site.register(visitor.client, **fields)
    assert response.status_code == 200 and message in response.get_data(as_text=True)
    assert site.sql("SELECT COUNT(*) AS n FROM users")[0]["n"] == 0


def test_registration_creates_a_hashed_requester_account(site):
    visitor = site.anonymous()
    response = site.register(visitor.client, "Alice Smith", "Alice@Example.com", password="password123")
    assert response.status_code == 302 and response.location.endswith("/home")
    row = site.sql("SELECT email, role, password_hash FROM users")[0]
    assert row["email"] == "alice@example.com"
    assert row["role"] == "requester"
    assert "password123" not in row["password_hash"] and len(row["password_hash"]) > 50


def test_new_account_is_signed_in_straight_away(alice):
    assert "Welcome, Alice" in alice.text("/home")


def test_duplicate_email_is_rejected(site, alice):
    other = site.anonymous()
    response = site.register(other.client, "Alice Other", "ALICE@example.com")
    assert "already exists" in response.get_data(as_text=True)


def test_logout_ends_the_session(alice):
    response = alice.post("/logout")
    assert response.status_code == 302 and "/login" in response.location
    assert alice.get("/home").status_code == 302


def test_wrong_password_and_unknown_email_give_the_same_message(site, alice):
    visitor = site.anonymous()
    token = site.token(visitor.client, "/login")
    wrong = visitor.client.post("/login", data=dict(csrf_token=token, email=alice.email, password="wrong-pass"))
    unknown = visitor.client.post("/login", data=dict(csrf_token=token, email="nobody@example.com", password="whatever1"))
    assert "Incorrect email or password" in wrong.get_data(as_text=True)
    assert "Incorrect email or password" in unknown.get_data(as_text=True)


def test_sign_in_ignores_email_case_and_honours_a_local_next_page(site, alice):
    visitor = site.anonymous()
    token = site.token(visitor.client, "/login")
    response = visitor.client.post("/login", data=dict(
        csrf_token=token, email="ALICE@Example.com", password="password123", next="/incidents/new"))
    assert response.status_code == 302 and response.location.endswith("/incidents/new")


@pytest.mark.parametrize("target", ["//evil.example", "https://evil.example", "/\\evil.example"])
def test_open_redirect_after_sign_in_is_blocked(site, alice, target):
    visitor = site.anonymous()
    token = site.token(visitor.client, "/login")
    response = visitor.client.post("/login", data=dict(
        csrf_token=token, email=alice.email, password="password123", next=target))
    assert response.status_code == 302 and "evil" not in response.location


def test_session_cookie_is_http_only_and_same_site(site):
    cookie = site.anonymous().get("/login").headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


def test_security_headers_and_no_caching_of_pages(alice):
    response = alice.get("/home")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-store"


def test_stylesheet_can_be_cached(site):
    response = site.anonymous().get("/static/style.css")
    assert response.status_code == 200 and response.headers.get("Cache-Control") != "no-store"


def test_unknown_page_when_signed_out_goes_to_sign_in_and_when_signed_in_shows_404(site, alice):
    assert site.anonymous().get("/nope").status_code == 302
    response = alice.get("/nope")
    assert response.status_code == 404 and "Not found" in response.get_data(as_text=True)


class TestPasswordChange:
    def test_wrong_current_password(self, alice):
        alice.post("/profile/password", dict(current="nope", new="newpassword1", confirm="newpassword1"))
        assert "current password is not correct" in alice.text("/profile")

    def test_too_short(self, alice):
        alice.post("/profile/password", dict(current="password123", new="short", confirm="short"))
        assert "at least 8" in alice.text("/profile")

    def test_mismatch(self, alice):
        alice.post("/profile/password", dict(current="password123", new="newpassword1", confirm="other-password"))
        assert "do not match" in alice.text("/profile")

    def test_success_and_new_password_works(self, site, alice):
        alice.post("/profile/password", dict(current="password123", new="newpassword1", confirm="newpassword1"))
        alice.post("/logout")
        visitor = site.anonymous()
        token = site.token(visitor.client, "/login")
        old = visitor.client.post("/login", data=dict(csrf_token=token, email=alice.email, password="password123"))
        new = visitor.client.post("/login", data=dict(csrf_token=token, email=alice.email, password="newpassword1"))
        assert old.status_code == 200 and new.status_code == 302 and new.location.endswith("/home")
