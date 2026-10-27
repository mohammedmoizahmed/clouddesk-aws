"""Sign-in, sessions and request protection.

There is no self-registration: accounts are created by an administrator.
"""
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


def test_sign_in_page_renders_and_offers_no_way_to_create_an_account(site):
    response = site.anonymous().get("/login")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "/register" not in html and "Create an account" not in html
    assert "created by an administrator" in html


class TestNoSelfRegistration:
    FIELDS = dict(full_name="Mallory Evil", email="mallory@evil.example", phone="+966551234567",
                  password="password123", confirm="password123")

    def test_page_is_gone_for_visitors(self, site):
        assert site.anonymous().get("/register").status_code in (302, 404)

    def test_page_is_gone_for_signed_in_users(self, alice):
        assert alice.get("/register").status_code == 404

    def test_posting_to_it_creates_nothing(self, site):
        visitor = site.anonymous()
        token = site.token(visitor.client, "/login")
        response = visitor.client.post("/register", data=dict(self.FIELDS, csrf_token=token))
        assert response.status_code in (302, 400, 404)
        assert site.sql("SELECT COUNT(*) AS n FROM users")[0]["n"] == 0

    def test_signed_in_requester_cannot_post_to_it_either(self, site, alice):
        response = alice.post("/register", self.FIELDS)
        assert response.status_code == 404
        assert site.sql("SELECT COUNT(*) AS n FROM users")[0]["n"] == 1


def test_post_without_csrf_token_is_rejected(site):
    visitor = site.anonymous()
    response = visitor.client.post("/login", data=dict(email="a@b.co", password="x"))
    assert response.status_code == 400


def test_an_existing_account_can_sign_in(site):
    site.make_user("Alice Smith", "alice@example.com", login=False)
    visitor = site.anonymous()
    response = site.login(visitor.client, "alice@example.com")
    assert response.status_code == 302 and response.location.endswith("/home")
    assert "Welcome, Alice" in visitor.text("/home")


def test_an_email_that_has_no_account_cannot_sign_in(site):
    visitor = site.anonymous()
    response = site.login(visitor.client, "nobody@example.com", "password123")
    assert response.status_code == 200 and "Incorrect email or password" in response.get_data(as_text=True)
    assert visitor.get("/home").status_code == 302


def test_wrong_password_and_unknown_email_give_the_same_message(site, alice):
    visitor = site.anonymous()
    wrong = site.login(visitor.client, alice.email, "wrong-pass")
    unknown = site.login(visitor.client, "nobody@example.com", "whatever1")
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


def test_logout_ends_the_session(alice):
    response = alice.post("/logout")
    assert response.status_code == 302 and "/login" in response.location
    assert alice.get("/home").status_code == 302


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


class TestDisabledAccounts:
    def test_correct_password_is_refused_with_a_clear_message(self, site):
        site.make_user("Dora Disabled", "dora@example.com", active=False, login=False)
        visitor = site.anonymous()
        response = site.login(visitor.client, "dora@example.com")
        assert response.status_code == 200 and "disabled" in response.get_data(as_text=True)
        assert visitor.get("/home").status_code == 302

    def test_wrong_password_does_not_reveal_that_the_account_is_disabled(self, site):
        site.make_user("Dora Disabled", "dora@example.com", active=False, login=False)
        response = site.login(site.anonymous().client, "dora@example.com", "wrong-pass")
        assert "Incorrect email or password" in response.get_data(as_text=True)

    def test_signed_in_user_is_cut_off_when_disabled(self, site, alice):
        assert alice.get("/home").status_code == 200
        site.sql("UPDATE users SET active=0 WHERE id=%s", (alice.id,))
        response = alice.get("/home")
        assert response.status_code == 302 and "/login" in response.location
        assert "disabled" in alice.text("/login")


class TestForcedPasswordChange:
    @pytest.fixture
    def newcomer(self, site):
        return site.make_user("Nina Newcomer", "nina@example.com", must_change=True, password="temp-pass-1")

    @pytest.mark.parametrize("page", ["/home", "/incidents", "/incidents/new", "/admin/users"])
    def test_other_pages_send_the_user_to_the_profile_page(self, newcomer, page):
        response = newcomer.get(page)
        assert response.status_code == 302 and response.location.endswith("/profile")

    def test_profile_page_and_sign_out_still_work(self, newcomer):
        assert newcomer.get("/profile").status_code == 200
        assert newcomer.post("/logout").status_code == 302

    def test_choosing_a_new_password_lifts_the_restriction(self, site, newcomer):
        newcomer.post("/profile/password", dict(current="temp-pass-1", new="my-own-pass-2", confirm="my-own-pass-2"))
        assert site.sql("SELECT must_change_password AS m FROM users")[0]["m"] == 0
        assert newcomer.get("/home").status_code == 200

    def test_the_temporary_password_cannot_be_reused(self, newcomer):
        newcomer.post("/profile/password", dict(current="temp-pass-1", new="temp-pass-1", confirm="temp-pass-1"))
        assert "different from your current one" in newcomer.text("/profile")
        assert newcomer.get("/home").status_code == 302


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
        old = site.login(site.anonymous().client, alice.email, "password123")
        new = site.login(site.anonymous().client, alice.email, "newpassword1")
        assert old.status_code == 200 and new.status_code == 302 and new.location.endswith("/home")

    def test_changing_the_password_signs_out_other_devices_but_not_this_one(self, site, alice):
        other_device = site.second_session(alice)
        assert other_device.get("/home").status_code == 200
        alice.post("/profile/password", dict(current="password123", new="newpassword1", confirm="newpassword1"))
        assert alice.get("/home").status_code == 200
        assert other_device.get("/home").status_code == 302
