import json
import time

import httpx
import pytest
import respx
from click.testing import CliRunner

from audioshake import AuthError, auth, cli
from audioshake.cli import _settings


def invoke(*args):
    return CliRunner().invoke(cli.main, list(args))


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    monkeypatch.setenv(auth.CONFIG_DIR_ENV, str(tmp_path))
    monkeypatch.delenv("AUDIOSHAKE_API_KEY", raising=False)
    monkeypatch.delenv("AUDIOSHAKE_BASE_URL", raising=False)
    return tmp_path


def _session(email="me@x.test", expires_in=3600):
    from tests.lib.test_auth import make_jwt

    return auth.TokenSet("acc", make_jwt({"email": email}), "ref", time.time() + expires_in)


class TestLogin:
    def test_login_prints_identity_and_caches(self, cfg, monkeypatch):
        seen = {}

        def fake_login(*, open_browser, timeout, on_url):
            seen.update(open_browser=open_browser, timeout=timeout)
            on_url("https://auth.test/oauth2/authorize?x=1")
            return _session()

        monkeypatch.setattr(auth, "login", fake_login)
        result = invoke("login", "--no-browser", "--timeout", "42")
        assert result.exit_code == 0, result.stderr
        assert seen == {"open_browser": False, "timeout": 42.0}
        assert "https://auth.test/oauth2/authorize" in result.stderr
        assert "Signed in as me@x.test" in result.stdout

    def test_the_sign_in_url_is_only_printed_when_asked(self, cfg, monkeypatch):
        def fake_login(*, open_browser, timeout, on_url):
            on_url("https://auth.test/oauth2/authorize?state=secret-state")
            return _session()

        monkeypatch.setattr(auth, "login", fake_login)
        quiet = invoke("login")
        assert quiet.exit_code == 0, quiet.stderr
        assert "secret-state" not in quiet.stderr and "re-run with -v" in quiet.stderr
        assert "secret-state" in invoke("-v", "login").stderr

    def test_login_failure_exits_3(self, cfg, monkeypatch):
        def failing(**kwargs):
            raise AuthError("Browser sign-in is not configured. Set ...")

        monkeypatch.setattr(auth, "login", failing)
        result = invoke("login")
        assert result.exit_code == 3
        assert "not configured" in result.stderr

    def test_login_warns_when_api_key_would_win(self, cfg, monkeypatch):
        monkeypatch.setenv("AUDIOSHAKE_API_KEY", "k")
        monkeypatch.setattr(auth, "login", lambda **kw: _session())
        result = invoke("login")
        assert "takes precedence" in result.stderr


class TestWhoami:
    def test_not_signed_in(self, cfg):
        result = invoke("whoami")
        assert result.exit_code == 3
        assert "Not signed in" in result.stdout

    def test_not_signed_in_with_json_still_prints_the_object(self, cfg):
        """--json keeps its object so a script can parse it, and still exits 3."""
        result = invoke("--json", "whoami")
        assert result.exit_code == 3
        assert json.loads(result.stdout) == {
            "credential": None,
            "signed_in_as": None,
            "expires_at": None,
        }

    def test_signed_in(self, cfg):
        auth.TokenStore().save(auth.store_key(), _session())
        result = invoke("whoami")
        assert result.exit_code == 0
        assert "me@x.test" in result.stdout and "valid until" in result.stdout
        as_json = invoke("--json", "whoami")
        assert as_json.exit_code == 0, as_json.stderr
        data = json.loads(as_json.stdout)
        assert data["credential"] == "session" and data["signed_in_as"] == "me@x.test"

    def test_api_key_takes_precedence(self, cfg, monkeypatch):
        auth.TokenStore().save(auth.store_key(), _session())
        monkeypatch.setenv("AUDIOSHAKE_API_KEY", "k")
        result = invoke("whoami")
        assert "AUDIOSHAKE_API_KEY" in result.stdout and "takes precedence" in result.stdout
        assert json.loads(invoke("--json", "whoami").stdout)["credential"] == "api_key"

    @pytest.mark.parametrize("api_key_set", [True, False], ids=["with key", "session only"])
    def test_an_unrefreshable_session_fails_only_when_it_is_the_sole_credential(
        self, cfg, monkeypatch, api_key_set
    ):
        auth.TokenStore().save(auth.store_key(), _session(expires_in=-60))
        if api_key_set:
            monkeypatch.setenv("AUDIOSHAKE_API_KEY", "k")
        with respx.mock(base_url="https://auth.test") as idp:
            idp.post("/oauth2/token").respond(400, json={"error": "invalid_grant"})
            result = invoke("whoami")
            as_json = invoke("--json", "whoami")

        # Why the session is gone belongs on stderr either way; a dead session
        # is not a credential, so --json reports none and exits 3 with the
        # object still printed.
        assert "could not be refreshed" in result.stderr
        assert json.loads(as_json.stdout) == {
            "credential": "api_key" if api_key_set else None,
            "signed_in_as": None,
            "expires_at": None,
        }
        if not api_key_set:
            assert result.exit_code == 3 and as_json.exit_code == 3
            return
        assert result.exit_code == 0, result.stderr
        assert as_json.exit_code == 0
        assert "Using the API key from AUDIOSHAKE_API_KEY" in result.stdout


class TestLogout:
    def test_logout_revokes_then_clears_session(self, cfg):
        auth.TokenStore().save(auth.store_key(), _session())
        with respx.mock(base_url="https://auth.test") as idp:
            route = idp.post("/oauth2/revoke").respond(200)
            assert invoke("logout").exit_code == 0
        assert route.called  # the session is ended at the provider, not just here
        assert auth.TokenStore().load(auth.store_key()) is None
        assert invoke("whoami").exit_code == 3

    def test_logout_survives_an_unreachable_provider(self, cfg):
        auth.TokenStore().save(auth.store_key(), _session())
        with respx.mock(base_url="https://auth.test") as idp:
            idp.post("/oauth2/revoke").mock(side_effect=httpx.ConnectError("down"))
            result = invoke("logout")
        assert result.exit_code == 0 and "Signed out" in result.stdout
        assert auth.TokenStore().load(auth.store_key()) is None


class TestCredentialResolution:
    def test_session_is_used_when_no_key(self, cfg, monkeypatch):
        auth.TokenStore().save(auth.store_key(), _session())
        captured = {}

        class FakeAudioShake:
            def __init__(self, api_key=None, **kwargs):
                captured.update(api_key=api_key, **kwargs)

        monkeypatch.setattr(_settings, "AudioShake", FakeAudioShake)
        _settings.Settings().client(retries=1)
        assert captured["api_key"] is None
        assert captured["bearer_token"] == "acc" and captured["retries"] == 1  # the access token

    def test_flag_beats_session(self, cfg, monkeypatch):
        auth.TokenStore().save(auth.store_key(), _session())
        captured = {}

        class FakeAudioShake:
            def __init__(self, api_key=None, **kwargs):
                captured.update(api_key=api_key, **kwargs)

        monkeypatch.setattr(_settings, "AudioShake", FakeAudioShake)
        _settings.Settings(api_key="flag").client()
        assert captured == {"api_key": "flag"}

    def test_nothing_available_is_an_auth_error(self, cfg):
        result = invoke("models")
        assert result.exit_code == 3
        # The hint names both ways in: sign in, or set the key.
        assert "audioshake login" in result.stderr and "AUDIOSHAKE_API_KEY" in result.stderr

    def test_expired_unrefreshable_session_is_an_auth_error(self, cfg, monkeypatch):
        monkeypatch.setenv(auth.DOMAIN_ENV, "auth.test")
        monkeypatch.setenv(auth.CLIENT_ID_ENV, "c")
        auth.TokenStore().save(auth.store_key(), auth.TokenSet("a", "i", "r", time.time() - 1))
        import respx

        with respx.mock(base_url="https://auth.test") as mock:
            mock.post("/oauth2/token").respond(400, json={"error": "invalid_grant"})
            result = invoke("models")
        assert result.exit_code == 3
        assert "sign in again" in result.stderr
