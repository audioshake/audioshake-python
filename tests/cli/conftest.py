import pytest


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch, tmp_path):
    """Never let a developer's real ~/.audioshake session or key leak into CLI tests.

    The sign-in domain is pointed at a reserved name as well, so nothing here
    can reach the real sign-in service (`logout` revokes over the network).
    """
    monkeypatch.setenv("AUDIOSHAKE_CONFIG_DIR", str(tmp_path / "audioshake-config"))
    monkeypatch.setenv("AUDIOSHAKE_SIGNIN_DOMAIN", "auth.test")
    monkeypatch.delenv("AUDIOSHAKE_API_KEY", raising=False)
    monkeypatch.delenv("AUDIOSHAKE_BASE_URL", raising=False)
