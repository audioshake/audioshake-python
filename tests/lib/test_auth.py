"""Tests for the browser sign-in flow. HTTP is mocked; the loopback server is real."""

import base64
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
import respx

from audioshake import AuthError, auth

DOMAIN = "auth.example.com"
CLIENT_ID = "client123"


def make_jwt(claims: dict) -> str:
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    return ".".join([b64(b'{"alg":"none"}'), b64(json.dumps(claims).encode()), b64(b"sig")])


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setenv(auth.DOMAIN_ENV, DOMAIN)
    monkeypatch.setenv(auth.CLIENT_ID_ENV, CLIENT_ID)
    monkeypatch.setenv(auth.CONFIG_DIR_ENV, str(tmp_path / "cfg"))
    return tmp_path / "cfg"


class TestConfig:
    def test_env_vars_configure_the_flow(self, configured):
        config = auth.get_signin_config()
        assert config.authorize_url == f"https://{DOMAIN}/oauth2/authorize"
        assert config.token_url == f"https://{DOMAIN}/oauth2/token"
        assert config.scopes == ("openid", "email", "profile")

    def test_the_token_host_may_differ_from_the_sign_in_page(self, monkeypatch):
        monkeypatch.delenv(auth.DOMAIN_ENV, raising=False)
        monkeypatch.delenv(auth.TOKEN_DOMAIN_ENV, raising=False)
        config = auth.get_signin_config()
        assert config.authorize_url == f"https://{auth.DEFAULT_SIGNIN_DOMAIN}/oauth2/authorize"
        assert config.token_url == f"https://{auth.DEFAULT_TOKEN_DOMAIN}/oauth2/token"

    @pytest.mark.parametrize(
        ("env", "value", "authorize_url", "token_url"),
        [
            (
                auth.TOKEN_DOMAIN_ENV,
                "tokens.example.com",
                f"https://{DOMAIN}/oauth2/authorize",
                "https://tokens.example.com/oauth2/token",
            ),
            (
                auth.DOMAIN_ENV,
                "http://localhost:9999/",
                "http://localhost:9999/oauth2/authorize",
                "http://localhost:9999/oauth2/token",
            ),
        ],
        ids=["token-host-override", "domain-with-scheme"],
    )
    def test_the_hosts_can_be_overridden(
        self, configured, monkeypatch, env, value, authorize_url, token_url
    ):
        monkeypatch.setenv(env, value)
        config = auth.get_signin_config()
        assert config.authorize_url == authorize_url
        assert config.token_url == token_url

    def test_missing_configuration_is_reported(self, monkeypatch):
        monkeypatch.delenv(auth.DOMAIN_ENV, raising=False)
        monkeypatch.delenv(auth.CLIENT_ID_ENV, raising=False)
        monkeypatch.setattr(auth, "DEFAULT_SIGNIN_CLIENT_ID", "")
        with pytest.raises(AuthError, match="not configured"):
            auth.get_signin_config()


class TestTokenSet:
    def test_repr_leaves_the_tokens_out(self):
        tokens = auth.TokenSet(
            access_token="ACCESS-SECRET", id_token="ID-SECRET", refresh_token="REFRESH-SECRET"
        )
        assert "SECRET" not in repr(tokens) and "SECRET" not in str(tokens)

    def test_expiry_uses_a_safety_margin(self):
        soon = auth.TokenSet("a", "i", expires_at=time.time() + auth.EXPIRY_SKEW_SECONDS - 5)
        later = auth.TokenSet("a", "i", expires_at=time.time() + auth.EXPIRY_SKEW_SECONDS + 60)
        assert soon.is_expired() and not later.is_expired()

    def test_describe_reads_the_email_claim_and_tolerates_garbage(self):
        assert auth.TokenSet("a", make_jwt({"email": "me@x.test"})).describe() == "me@x.test"
        assert auth.TokenSet("a", "not.a.jwt").describe() == "unknown user"
        assert auth.decode_jwt_claims("garbage") == {}
        assert auth.decode_jwt_claims("") == {}

    def test_round_trip_dict(self):
        original = auth.TokenSet("a", "i", "r", 12.5)
        assert auth.TokenSet.from_dict(original.to_dict()) == original


class TestTokenStore:
    def test_round_trips_per_key_with_private_permissions(self, tmp_path):
        store = auth.TokenStore(tmp_path)
        store.save("api.audioshake.ai", auth.TokenSet("a", "i", "r", 1.0))
        store.save("other.host", auth.TokenSet("b", "j"))
        store.save("api.audioshake.ai", auth.TokenSet("a2", "i2"))  # replaces, not appends
        assert store.load("api.audioshake.ai").access_token == "a2"
        assert store.load("missing") is None
        assert [p.name for p in store.config_dir.iterdir()] == [store.path.name]  # no temp file
        assert oct(store.path.stat().st_mode & 0o777) == "0o600"
        assert oct(store.config_dir.stat().st_mode & 0o777) == "0o700"
        assert sorted(store.keys()) == ["api.audioshake.ai", "other.host"]
        store.clear("api.audioshake.ai")
        assert store.load("api.audioshake.ai") is None and store.load("other.host") is not None
        store.clear()
        assert not store.path.exists()

    def test_a_failed_write_keeps_the_previous_file(self, tmp_path, monkeypatch):
        store = auth.TokenStore(tmp_path)
        store.save("api.audioshake.ai", auth.TokenSet("a", "i", "r", 1.0))
        before = store.path.read_bytes()

        def explode(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(auth.json, "dump", explode)
        with pytest.raises(OSError, match="disk full"):
            store.save("other.host", auth.TokenSet("b", "j"))

        assert store.path.read_bytes() == before  # nothing truncated or half-written
        assert [p.name for p in store.config_dir.iterdir()] == [store.path.name]  # no temp left

    def test_corrupt_file_is_treated_as_empty(self, tmp_path):
        store = auth.TokenStore(tmp_path)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text("{not json")
        assert store.load("x") is None

    def test_store_key_is_the_api_host(self, monkeypatch):
        monkeypatch.delenv("AUDIOSHAKE_BASE_URL", raising=False)
        assert auth.store_key() == "api.audioshake.ai"
        assert auth.store_key("https://api.example.test:8443/v1") == "api.example.test:8443"


class TestTokenErrors:
    @pytest.fixture
    def idp(self, configured):
        with respx.mock(base_url=f"https://{DOMAIN}") as mock:
            yield mock

    @pytest.mark.parametrize(
        ("response", "match"),
        [
            ({"status_code": 400, "json": {"error": "invalid_grant"}}, "invalid_grant"),
            ({"status_code": 403, "text": "Forbidden"}, "HTTP 403"),
        ],
        ids=["oauth-error-quoted", "bare-status-reported"],
    )
    def test_token_endpoint_errors_are_reported(self, idp, response, match):
        idp.post("/oauth2/token").respond(**response)
        with pytest.raises(AuthError, match=match):
            auth.refresh(
                auth.TokenSet("a", "i", refresh_token="r"), store=auth.TokenStore("/tmp/x")
            )

    def test_no_refresh_token(self, configured):
        with pytest.raises(AuthError, match="sign in again"):
            auth.refresh(auth.TokenSet("a", "i"))


def test_callback_page_escapes_what_it_echoes():
    page = auth._callback_page("Sign-in failed", '<script>alert(1)</script> & "x"')
    assert "<script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;x&quot;" in page


class TestLoginFlow:
    """Drives the real loopback server: a thread plays the browser and hits the callback."""

    @pytest.fixture
    def idp(self, configured, monkeypatch):
        monkeypatch.setattr(auth.webbrowser, "open", lambda url: True)
        with respx.mock(base_url=f"https://{DOMAIN}", assert_all_called=False) as mock:
            yield mock

    def _visit(self, url_holder, mutate=None):
        def browser():
            for _ in range(200):
                if url_holder:
                    break
                time.sleep(0.01)
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url_holder[0]).query))
            params = {"code": "auth-code", "state": query["state"]}
            if mutate:
                params = mutate(params)
            # urllib, not httpx: respx intercepts httpx inside these tests. The
            # server binds 127.0.0.1, so skip the browser's localhost -> ::1 dance.
            callback = query["redirect_uri"].replace("localhost", "127.0.0.1")
            for _ in range(50):
                try:
                    urllib.request.urlopen(
                        f"{callback}?{urllib.parse.urlencode(params)}", timeout=1
                    )
                    return
                except OSError:
                    time.sleep(0.02)

        return threading.Thread(target=browser, daemon=True)

    def _post_back(self, url_holder, fields, origin=None, statuses=None):
        """Play the sign-in page posting tokens straight to the loopback server."""

        def browser():
            for _ in range(200):
                if url_holder:
                    break
                time.sleep(0.01)
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url_holder[0]).query))
            body = urllib.parse.urlencode({"state": query["state"], **fields}).encode()
            callback = query["redirect_uri"].replace("localhost", "127.0.0.1")
            headers = {"Origin": origin} if origin else {}
            request = urllib.request.Request(callback, data=body, headers=headers)
            for _ in range(50):
                try:
                    with urllib.request.urlopen(request, timeout=1) as response:
                        if statuses is not None:
                            statuses.append(response.status)
                    return
                except urllib.error.HTTPError as e:
                    e.close()  # the error holds the response open
                    if statuses is not None:
                        statuses.append(e.code)
                    return
                except OSError:
                    time.sleep(0.02)

        return threading.Thread(target=browser, daemon=True)

    def test_successful_login_exchanges_code_and_caches_tokens(self, idp, configured):
        route = idp.post("/oauth2/token").respond(
            200,
            json={
                "access_token": "acc",
                "id_token": make_jwt({"email": "me@x.test"}),
                "refresh_token": "ref",
                "expires_in": 3600,
            },
        )
        seen_urls = []
        self._visit(seen_urls).start()
        tokens = auth.login(on_url=seen_urls.append, timeout=10)

        assert tokens.describe() == "me@x.test" and tokens.refresh_token == "ref"
        sent = dict(urllib.parse.parse_qsl(route.calls.last.request.content.decode()))
        assert sent["grant_type"] == "authorization_code" and sent["code"] == "auth-code"
        assert sent["client_id"] == CLIENT_ID and sent["redirect_uri"].startswith(
            "http://localhost:"
        )
        assert route.calls.last.request.headers["user-agent"].startswith("audioshake-python/")
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(seen_urls[0]).query))
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(sent["code_verifier"].encode()).digest())
            .decode()
            .rstrip("=")
        )
        assert query["code_challenge"] == expected and query["code_challenge_method"] == "S256"
        assert auth.TokenStore().load(auth.store_key()).access_token == "acc"
        assert (Path(configured) / "credentials.json").exists()

    def test_tokens_posted_by_the_page_are_taken_as_is(self, idp, configured):
        route = idp.post("/oauth2/token")
        seen = []
        self._post_back(
            seen,
            {
                "token_type": "Bearer",
                "access_token": "acc",
                "id_token": make_jwt({"email": "me@x.test", "aud": CLIENT_ID}),
                "refresh_token": "ref",
                "expires_in": "3600",  # a form post carries strings
            },
            origin=f"https://{DOMAIN}",
        ).start()
        tokens = auth.login(on_url=seen.append, timeout=10)

        assert tokens.describe() == "me@x.test" and tokens.refresh_token == "ref"
        assert not tokens.is_expired()
        assert not route.called  # nothing left to exchange
        assert auth.TokenStore().load(auth.store_key()).access_token == "acc"

    def test_a_posted_state_must_still_match(self, idp):
        seen = []
        self._post_back(
            seen,
            {"access_token": "acc", "id_token": "i", "state": "forged"},
            origin=f"https://{DOMAIN}",
        ).start()
        with pytest.raises(AuthError, match="state did not match"):
            auth.login(on_url=seen.append, timeout=10)

    @pytest.mark.parametrize(
        "origin", ["https://evil.test", None], ids=["another-origin", "no-origin-header"]
    )
    def test_tokens_posted_from_anywhere_but_the_sign_in_page_are_refused(self, idp, origin):
        # Only the sign-in page's origin may post tokens; a post with no Origin
        # header did not come from a browser at all.
        seen, statuses = [], []
        self._post_back(
            seen, {"access_token": "acc", "id_token": "i"}, origin=origin, statuses=statuses
        ).start()
        with pytest.raises(AuthError, match="Timed out"):
            auth.login(on_url=seen.append, timeout=2)
        assert statuses == [403]
        assert auth.TokenStore().load(auth.store_key()) is None

    @pytest.mark.parametrize(
        "fields",
        [
            {"access_token": "acc"},
            {"access_token": "acc", "id_token": make_jwt({"email": "me@x.test"})},
            {"access_token": "acc", "id_token": make_jwt({"aud": "someone-elses-app"})},
        ],
        ids=["no-id-token", "no-audience", "another-application"],
    )
    def test_posted_tokens_must_name_this_application(self, idp, fields):
        seen = []
        self._post_back(seen, fields, origin=f"https://{DOMAIN}").start()
        with pytest.raises(AuthError, match="not issued for this application"):
            auth.login(on_url=seen.append, timeout=10)
        assert auth.TokenStore().load(auth.store_key()) is None

    def test_tokens_in_a_callback_link_are_refused_and_sign_in_keeps_waiting(self, idp):
        idp.post("/oauth2/token").respond(
            200,
            json={"access_token": "real", "id_token": make_jwt({"email": "me@x.test"})},
        )
        seen, statuses = [], []

        def browser():
            for _ in range(200):
                if seen:
                    break
                time.sleep(0.01)
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(seen[0]).query))
            callback = query["redirect_uri"].replace("localhost", "127.0.0.1")
            forged = {"state": query["state"], "access_token": "attacker"}
            genuine = {"state": query["state"], "code": "auth-code"}
            for params in (forged, genuine):
                for _ in range(50):
                    try:
                        with urllib.request.urlopen(
                            f"{callback}?{urllib.parse.urlencode(params)}", timeout=1
                        ) as response:
                            statuses.append(response.status)
                        break
                    except urllib.error.HTTPError as e:
                        e.close()
                        statuses.append(e.code)
                        break
                    except OSError:
                        time.sleep(0.02)

        threading.Thread(target=browser, daemon=True).start()
        tokens = auth.login(on_url=seen.append, timeout=10)

        assert statuses == [400, 200]
        assert tokens.access_token == "real"  # from the code exchange, not the link

    def test_html_from_the_token_endpoint_is_reported(self, idp):
        idp.post("/oauth2/token").respond(200, html="<!doctype html><html></html>")
        seen = []
        self._visit(seen).start()
        with pytest.raises(AuthError, match="did not return a token"):
            auth.login(on_url=seen.append, timeout=10)

    def test_mismatched_state_is_rejected(self, idp):
        seen = []
        self._visit(seen, lambda p: {**p, "state": "forged"}).start()
        with pytest.raises(AuthError, match="state did not match"):
            auth.login(on_url=seen.append, timeout=10)
        assert not idp.calls

    def test_provider_error_is_surfaced(self, idp):
        seen = []
        self._visit(
            seen,
            lambda p: {"error": "access_denied", "error_description": "nope", "state": p["state"]},
        ).start()
        with pytest.raises(AuthError, match="nope"):
            auth.login(on_url=seen.append, timeout=10)


class TestGetValidToken:
    @pytest.fixture
    def idp(self, configured):
        with respx.mock(base_url=f"https://{DOMAIN}", assert_all_called=False) as mock:
            yield mock

    def test_nothing_cached(self, configured):
        assert auth.get_valid_token() is None

    def test_fresh_token_is_returned_without_a_request(self, idp):
        store = auth.TokenStore()
        store.save(auth.store_key(), auth.TokenSet("a", "i", "r", time.time() + 3600))
        assert auth.get_valid_token().access_token == "a"
        assert not idp.calls

    def test_expired_token_is_refreshed_and_recached(self, idp):
        idp.post("/oauth2/token").respond(
            200, json={"access_token": "a2", "id_token": "i2", "expires_in": 3600}
        )
        store = auth.TokenStore()
        store.save(auth.store_key(), auth.TokenSet("a", "i", "r", time.time() - 1))
        tokens = auth.get_valid_token()
        assert tokens.access_token == "a2" and tokens.refresh_token == "r"  # kept from before
        assert 3500 < tokens.expires_at - time.time() <= 3600  # expires_in honoured
        assert store.load(auth.store_key()).access_token == "a2"

    def test_failed_refresh_asks_to_sign_in_again(self, idp):
        idp.post("/oauth2/token").respond(400, json={"error": "invalid_grant"})
        auth.TokenStore().save(auth.store_key(), auth.TokenSet("a", "i", "r", time.time() - 1))
        with pytest.raises(AuthError, match="sign in again"):
            auth.get_valid_token()

    def test_logout_revokes_the_refresh_token(self, idp):
        route = idp.post("/oauth2/revoke").respond(200)
        store = auth.TokenStore()
        store.save(auth.store_key(), auth.TokenSet("a", "i", "ref"))

        auth.logout()

        sent = dict(urllib.parse.parse_qsl(route.calls.last.request.content.decode()))
        assert sent == {"client_id": CLIENT_ID, "token": "ref"}
        assert store.load(auth.store_key()) is None

    def test_logout_still_clears_when_revocation_is_refused(self, idp):
        # A provider with revocation turned off must not strand the session.
        idp.post("/oauth2/revoke").respond(400, json={"error": "unsupported_operation"})
        store = auth.TokenStore()
        store.save(auth.store_key(), auth.TokenSet("a", "i", "ref"))

        auth.logout()

        assert store.load(auth.store_key()) is None

    @pytest.mark.parametrize(
        ("tokens", "kwargs"),
        [
            (auth.TokenSet("a", "i"), {}),
            (auth.TokenSet("a", "i", "ref"), {"revoke": False}),
        ],
        ids=["nothing-to-revoke", "revocation-off"],
    )
    def test_logout_without_a_revoke_call_still_clears(self, idp, tokens, kwargs):
        route = idp.post("/oauth2/revoke")
        store = auth.TokenStore()
        store.save(auth.store_key(), tokens)

        auth.logout(**kwargs)

        assert not route.called
        assert store.load(auth.store_key()) is None

    def test_logout_all_revokes_every_host(self, idp):
        route = idp.post("/oauth2/revoke").respond(200)
        store = auth.TokenStore()
        store.save("api.one", auth.TokenSet("a", "i", "ref-one"))
        store.save("api.two", auth.TokenSet("b", "j", "ref-two"))

        auth.logout(everything=True)

        sent = [
            dict(urllib.parse.parse_qsl(call.request.content.decode()))["token"]
            for call in route.calls
        ]
        assert sorted(sent) == ["ref-one", "ref-two"]
        assert store.keys() == []
