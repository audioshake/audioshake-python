"""Browser sign-in for the CLI and other interactive callers.

Implements the OAuth 2.0 authorization code flow with PKCE against the AudioShake
sign-in page:

1. Start a loopback HTTP server to receive the redirect.
2. Open the browser at the hosted sign-in page.
3. The page comes back one of two ways: a redirect carrying an authorization
   code, which is exchanged for tokens at the token endpoint, or a form post
   carrying the tokens themselves. There is no client secret; PKCE proves the
   exchange comes from the process that started the flow.
4. Cache the tokens under ``~/.audioshake`` so later runs skip the browser, and
   refresh them silently while the refresh token is valid.

The resulting token is sent to the API as ``Authorization: Bearer <token>``.
Pass it to :class:`audioshake.AudioShake` as ``bearer_token``.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import html
import json
import logging
import os
import secrets
import tempfile
import time
import urllib.parse
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx

from . import _messages
from ._http import API_KEY_ENV, DEFAULT_BASE_URL, USER_AGENT
from .exceptions import AuthError
from .types import StrPath

__all__ = [
    "SignInConfig",
    "TokenSet",
    "TokenStore",
    "decode_jwt_claims",
    "get_signin_config",
    "get_valid_token",
    "login",
    "logout",
    "refresh",
]

_log = logging.getLogger("audioshake.auth")

# Sign-in settings. The app client is public and protected by PKCE, so none of
# these values are secrets. Environment variables override them for other
# deployments. The sign-in page hosts only the browser half of the flow, so the
# authorization code it hands back is exchanged with the identity provider that
# issued it, on a different host.
DEFAULT_SIGNIN_DOMAIN = "cli.audioshake.ai"
DEFAULT_TOKEN_DOMAIN = "audioshake-dashboard.auth.us-east-1.amazoncognito.com"
DEFAULT_SIGNIN_CLIENT_ID = "oou2n7071a308gt90vmgr1ilc"
SCOPES = ("openid", "email", "profile")

DOMAIN_ENV = "AUDIOSHAKE_SIGNIN_DOMAIN"
TOKEN_DOMAIN_ENV = "AUDIOSHAKE_SIGNIN_TOKEN_DOMAIN"
CLIENT_ID_ENV = "AUDIOSHAKE_SIGNIN_CLIENT_ID"
CONFIG_DIR_ENV = "AUDIOSHAKE_CONFIG_DIR"

DEFAULT_CONFIG_DIR = Path.home() / ".audioshake"
CREDENTIALS_FILENAME = "credentials.json"

# Loopback ports tried in order. Each must be registered as a callback URL on
# the app client (redirect URIs are matched exactly), so the list is fixed.
DEFAULT_REDIRECT_PORTS = tuple(range(51739, 51749))  # 51739-51748

# The registered callbacks are http://localhost:<port>/callback and redirect
# URIs are matched exactly, so the URI says "localhost" even though the server
# binds to the numeric loopback address to stay off the network.
REDIRECT_HOST = "localhost"
BIND_HOST = "127.0.0.1"

# The sign-in page posts a handful of JWTs back; anything larger is not ours.
MAX_CALLBACK_BODY_BYTES = 64 * 1024

# Refresh this many seconds before actual expiry so a long run does not have a
# token expire mid-flight.
EXPIRY_SKEW_SECONDS = 120
LOGIN_TIMEOUT_SECONDS = 300
TOKEN_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# Revocation is best effort, so logging out must not hang waiting for it.
REVOKE_TIMEOUT = httpx.Timeout(5.0, connect=5.0)


@dataclass(frozen=True)
class SignInConfig:
    """Sign-in page and app client settings."""

    domain: str
    client_id: str
    # Where the code is exchanged; empty means the sign-in domain does both.
    token_domain: str = ""
    scopes: tuple[str, ...] = SCOPES
    redirect_ports: tuple[int, ...] = DEFAULT_REDIRECT_PORTS

    @staticmethod
    def _as_base(domain: str) -> str:
        domain = domain.rstrip("/")
        return domain if domain.startswith(("http://", "https://")) else f"https://{domain}"

    @property
    def base(self) -> str:
        """The sign-in page's base URL."""
        return self._as_base(self.domain)

    @property
    def token_base(self) -> str:
        """The base URL that tokens are exchanged at."""
        return self._as_base(self.token_domain or self.domain)

    @property
    def authorize_url(self) -> str:
        """Where the browser is sent to sign in."""
        return f"{self.base}/oauth2/authorize"

    @property
    def token_url(self) -> str:
        """Where an authorization code or refresh token is exchanged."""
        return f"{self.token_base}/oauth2/token"

    @property
    def revoke_url(self) -> str:
        """Where a refresh token is revoked on logout."""
        return f"{self.token_base}/oauth2/revoke"


def get_signin_config() -> SignInConfig:
    """Resolve sign-in settings from the environment, falling back to the defaults.

    Raises:
        AuthError: No domain or client id is available.
    """
    domain = os.environ.get(DOMAIN_ENV) or DEFAULT_SIGNIN_DOMAIN
    client_id = os.environ.get(CLIENT_ID_ENV) or DEFAULT_SIGNIN_CLIENT_ID
    # A deployment that names its own sign-in page and nothing else serves the
    # whole flow from that one domain; only the default pair is split.
    token_domain = os.environ.get(TOKEN_DOMAIN_ENV) or (
        "" if os.environ.get(DOMAIN_ENV) else DEFAULT_TOKEN_DOMAIN
    )
    if not domain or not client_id:
        raise AuthError(_messages.signin_not_configured(DOMAIN_ENV, CLIENT_ID_ENV, API_KEY_ENV))
    return SignInConfig(domain=domain, client_id=client_id, token_domain=token_domain)


@dataclass
class TokenSet:
    """Tokens returned by the sign-in page.

    The tokens are left out of ``repr()``, so printing or logging a session,
    or a traceback that shows one, never reveals a credential.
    """

    access_token: str = field(repr=False)
    id_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: float = 0.0

    @property
    def api_token(self) -> str:
        """The token sent as the bearer credential on API requests."""
        return self.access_token

    def is_expired(self, skew: float = EXPIRY_SKEW_SECONDS) -> bool:
        """True if the access token expires within ``skew`` seconds."""
        return time.time() >= (self.expires_at - skew)

    @property
    def claims(self) -> dict[str, Any]:
        """The id token's claims, decoded without verification; for display only."""
        return decode_jwt_claims(self.id_token)

    def describe(self) -> str:
        """Who is signed in, best effort, for display."""
        claims = self.claims
        who = claims.get("email") or claims.get("sub")
        return str(who) if who else "unknown user"

    @classmethod
    def from_token_response(
        cls, payload: Mapping[str, Any], previous_refresh_token: str | None
    ) -> TokenSet:
        """Build a token set from the token endpoint's answer."""
        # A form post from the sign-in page carries strings, not numbers.
        try:
            lifetime = float(payload.get("expires_in", 3600))
        except (TypeError, ValueError):
            lifetime = 3600.0
        # Refresh responses omit the refresh token; keep the one already held.
        return cls(
            access_token=str(payload.get("access_token", "")),
            id_token=str(payload.get("id_token", "")),
            refresh_token=payload.get("refresh_token") or previous_refresh_token,
            expires_at=time.time() + lifetime,
        )

    def to_dict(self) -> dict[str, Any]:
        """The form stored in the credentials file."""
        return {
            "access_token": self.access_token,
            "id_token": self.id_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TokenSet:
        """Read a token set back from the credentials file."""
        return cls(
            access_token=str(data.get("access_token", "")),
            id_token=str(data.get("id_token", "")),
            refresh_token=data.get("refresh_token"),
            expires_at=float(data.get("expires_at", 0.0)),
        )


class TokenStore:
    """Cached tokens on disk, keyed by the API host they were issued for."""

    config_dir: Path
    path: Path

    def __init__(self, config_dir: StrPath | None = None) -> None:
        if config_dir is None:
            override = os.environ.get(CONFIG_DIR_ENV)
            config_dir = Path(override) if override else DEFAULT_CONFIG_DIR
        self.config_dir = Path(config_dir)
        self.path = self.config_dir / CREDENTIALS_FILENAME

    def load(self, key: str) -> TokenSet | None:
        """The cached session for an API host, or None."""
        entry = self._read_all().get(key)
        return TokenSet.from_dict(entry) if isinstance(entry, dict) else None

    def save(self, key: str, tokens: TokenSet) -> None:
        """Cache a session for an API host."""
        data = self._read_all()
        data[key] = tokens.to_dict()
        self._write_all(data)

    def clear(self, key: str | None = None) -> None:
        """Drop the tokens for one host, or the whole file."""
        if key is None:
            self.path.unlink(missing_ok=True)
            return
        data = self._read_all()
        if data.pop(key, None) is not None:
            self._write_all(data)

    def _read_all(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            _log.warning(_messages.CREDENTIALS_UNREADABLE, self.path, e)
            return {}
        return data if isinstance(data, dict) else {}

    def keys(self) -> list[str]:
        """Every API host with a cached session."""
        return list(self._read_all())

    def _write_all(self, data: dict[str, Any]) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # An existing directory keeps whatever mode it was made with, and only
        # this user has any business reading a session out of it. Permission
        # bits are not meaningful everywhere, so a failure here is not fatal.
        with contextlib.suppress(OSError):
            self.config_dir.chmod(0o700)
        # Write beside the file and rename over it: a reader sees the old file
        # or the new one, never a truncated one, so a failure part-way keeps
        # every cached session. mkstemp creates the file with mode 0600, so no
        # token bytes ever land in a file another user could read.
        fd, tmp_name = tempfile.mkstemp(dir=self.config_dir, prefix=".credentials-", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)


def store_key(base_url: str | None = None) -> str:
    """The cache key for an API base URL: its host."""
    url = base_url or os.environ.get("AUDIOSHAKE_BASE_URL") or DEFAULT_BASE_URL
    return urllib.parse.urlparse(url).netloc or url


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying it; the API verifies the signature."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _generate_pkce_pair() -> tuple[str, str]:
    """Return a (verifier, challenge) pair for PKCE S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


class _CallbackServer(HTTPServer):
    result: dict[str, str] | None = None
    # Origin the sign-in page posts from; anything else is not part of the flow.
    expected_origin: str = ""


class _CallbackHandler(BaseHTTPRequestHandler):
    """Single-shot handler that captures the return from the sign-in page.

    The page comes back either as a GET whose query carries an authorization
    code, or as a form POST whose body carries the tokens themselves.
    """

    server: _CallbackServer

    def do_GET(self) -> None:
        # The code flow redirects back with only a code and the state. Tokens
        # are accepted from a POST by the sign-in page, where the origin is
        # checked; a GET carrying them could come from anything that learned
        # the state, so it is refused and the sign-in keeps waiting.
        result = _parse_form(urllib.parse.urlparse(self.path).query)
        if _TOKEN_FIELDS & result.keys():
            self._respond(
                400,
                _callback_page(_messages.CALLBACK_FAILED_HEADING, _messages.CALLBACK_TOKENS_IN_URL),
            )
            return
        self._finish(result)

    def do_POST(self) -> None:
        # Any local process can post to a loopback port, and a browser always
        # labels a cross-origin post with its own origin. Requiring the sign-in
        # page's origin keeps the flow to that page even if the state leaks, and
        # a post carrying no Origin at all did not come from a browser.
        expected = self.server.expected_origin
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if expected and origin.casefold() != expected.casefold():
            self._respond(
                403,
                _callback_page(
                    _messages.CALLBACK_FAILED_HEADING, _messages.CALLBACK_UNEXPECTED_ORIGIN
                ),
            )
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(min(length, MAX_CALLBACK_BODY_BYTES)) if length > 0 else b""
        self._finish(_parse_form(body.decode("utf-8", "replace")))

    def _finish(self, result: dict[str, str]) -> None:
        self.server.result = result

        if "error" in result:
            body = _callback_page(
                _messages.CALLBACK_FAILED_HEADING,
                result.get("error_description", result["error"]),
            )
        elif "code" in result or "access_token" in result:
            body = _callback_page(
                _messages.CALLBACK_SIGNED_IN_HEADING, _messages.CALLBACK_SIGNED_IN
            )
        else:
            body = _callback_page(
                _messages.CALLBACK_FAILED_HEADING, _messages.CALLBACK_NO_CODE_OR_TOKEN
            )

        self._respond(200, body)

    def _respond(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default stderr request logging."""


def _parse_form(data: str) -> dict[str, str]:
    """First value of each field in a urlencoded query string or form body."""
    return {key: values[0] for key, values in urllib.parse.parse_qs(data).items()}


def _callback_page(heading: str, message: str) -> str:
    # The message can echo query parameters from the redirect; escape it.
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>AudioShake</title></head>'
        '<body style="font-family: system-ui, sans-serif; text-align: center; padding: 4rem;">'
        f"<h1>{html.escape(heading)}</h1><p>{html.escape(message)}</p></body></html>"
    )


_TOKEN_FIELDS = frozenset({"access_token", "id_token", "refresh_token"})
"""Callback fields that carry a credential, accepted only in a POST from the sign-in page."""


def _start_callback_server(ports: tuple[int, ...], expected_origin: str = "") -> _CallbackServer:
    last_error: OSError | None = None
    for port in ports:
        try:
            server = _CallbackServer((BIND_HOST, port), _CallbackHandler)
            server.expected_origin = expected_origin
            return server
        except OSError as e:
            last_error = e
    raise AuthError(_messages.callback_server_unavailable(ports, last_error))


def _wait_for_callback(server: _CallbackServer, timeout: float) -> dict[str, str]:
    server.timeout = 1
    deadline = time.time() + timeout
    while server.result is None:
        if time.time() > deadline:
            raise AuthError(_messages.signin_timeout(timeout))
        try:
            server.handle_request()
        except TimeoutError:  # pragma: no cover - depends on the socket implementation
            continue
    return server.result


def _post_token_request(
    config: SignInConfig, payload: dict[str, str], previous_refresh_token: str | None = None
) -> TokenSet:
    try:
        response = httpx.post(
            config.token_url,
            data=payload,
            headers={"User-Agent": USER_AGENT},
            timeout=TOKEN_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise AuthError(_messages.signin_unreachable(e)) from e
    if not response.is_success:
        raise AuthError(_describe_token_error(response))
    try:
        body: Any = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        content_type = response.headers.get("content-type", _messages.NO_CONTENT_TYPE)
        raise AuthError(
            _messages.token_endpoint_not_json(config.token_url, content_type, TOKEN_DOMAIN_ENV)
        )
    return TokenSet.from_token_response(body, previous_refresh_token)


def _check_audience(tokens: TokenSet, client_id: str) -> None:
    """Reject tokens that were minted for some other application.

    The exchanged-code path gets this for free: the provider only trades a code
    that was bound to this flow's PKCE challenge. Tokens posted straight to the
    loopback server carry no such proof, so check what the id token says about
    itself, and require it to name this application. The signature is verified
    by the API, not here, so this only rules out a token that never claimed to
    be ours.
    """
    audience = tokens.claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    # A token that names no audience at all, or comes without an id token,
    # makes no claim to be ours, so it is refused rather than given the benefit.
    if client_id not in audiences:
        raise AuthError(_messages.signin_wrong_audience())


def _describe_token_error(response: httpx.Response) -> str:
    try:
        data = response.json()
        detail = data.get("error_description") or data.get("error")
        if detail:
            return _messages.token_request_rejected(detail)
    except ValueError:
        pass
    return _messages.token_request_rejected(f"HTTP {response.status_code}")


def _revoke(refresh_token: str) -> bool:
    """Ask the provider to invalidate a refresh token. Best effort.

    Deleting the cached file ends the session on this machine; revoking ends it
    everywhere, so a copy of the credentials file cannot be used to carry on.
    A provider may have revocation turned off and logging out has to succeed
    either way, so a failure is reported and swallowed.
    """
    try:
        config = get_signin_config()
    except AuthError as e:
        _log.info("not revoking the refresh token: %s", e)
        return False
    try:
        response = httpx.post(
            config.revoke_url,
            data={"client_id": config.client_id, "token": refresh_token},
            headers={"User-Agent": USER_AGENT},
            timeout=REVOKE_TIMEOUT,
        )
    except httpx.HTTPError as e:
        _log.info("could not reach the revoke endpoint (%s); the token stays valid", e)
        return False
    if not response.is_success:
        _log.info(
            "the sign-in service declined the revoke request (HTTP %s); the refresh "
            "token stays valid until it expires",
            response.status_code,
        )
        return False
    _log.info("revoked the cached refresh token")
    return True


def login(
    *,
    open_browser: bool = True,
    store: TokenStore | None = None,
    key: str | None = None,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
    on_url: Callable[[str], object] | None = None,
) -> TokenSet:
    """Run the browser sign-in flow and cache the resulting tokens.

    Args:
        open_browser: Open the system browser; when False only ``on_url`` is called.
        store: Where to cache tokens. Defaults to ``~/.audioshake``.
        key: Cache key; defaults to the API host.
        timeout: Seconds to wait for the browser round trip.
        on_url: Receives the sign-in URL so a caller can print it.

    Raises:
        AuthError: The flow could not be completed.
    """
    config = get_signin_config()
    store = store or TokenStore()
    key = key or store_key()

    verifier, challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(24)
    server = _start_callback_server(config.redirect_ports, expected_origin=config.base)
    redirect_uri = f"http://{REDIRECT_HOST}:{server.server_port}/callback"
    authorize_url = f"{config.authorize_url}?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(config.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )

    try:
        if on_url is not None:
            on_url(authorize_url)
        if open_browser:
            webbrowser.open(authorize_url)
        result = _wait_for_callback(server, timeout)
    finally:
        server.server_close()

    if "error" in result:
        raise AuthError(
            _messages.signin_provider_error(result.get("error_description", result["error"]))
        )
    # Constant-time compare: the state is the CSRF guard for the callback.
    if not secrets.compare_digest(result.get("state", ""), state):
        raise AuthError(_messages.signin_state_mismatch())
    if result.get("access_token"):
        # The sign-in page completed the sign-in itself and posted the tokens
        # back to the loopback server; there is no code left to exchange.
        tokens = TokenSet.from_token_response(result, None)
        _check_audience(tokens, config.client_id)
    else:
        code = result.get("code")
        if not code:
            raise AuthError(_messages.signin_no_code())
        tokens = _post_token_request(
            config,
            {
                "grant_type": "authorization_code",
                "client_id": config.client_id,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )
    store.save(key, tokens)
    _log.info("signed in as %s", tokens.describe())
    return tokens


def refresh(
    tokens: TokenSet, *, store: TokenStore | None = None, key: str | None = None
) -> TokenSet:
    """Exchange the refresh token for a fresh token set and cache it."""
    if not tokens.refresh_token:
        raise AuthError(_messages.NO_REFRESH_TOKEN)
    config = get_signin_config()
    store = store or TokenStore()
    refreshed = _post_token_request(
        config,
        {
            "grant_type": "refresh_token",
            "client_id": config.client_id,
            "refresh_token": tokens.refresh_token,
        },
        previous_refresh_token=tokens.refresh_token,
    )
    store.save(key or store_key(), refreshed)
    return refreshed


def get_valid_token(*, store: TokenStore | None = None, key: str | None = None) -> TokenSet | None:
    """Return cached tokens, refreshing them if expired.

    Returns:
        The tokens, or ``None`` when nothing is cached.

    Raises:
        AuthError: The cached session has expired and cannot be refreshed.
    """
    store = store or TokenStore()
    key = key or store_key()
    tokens = store.load(key)
    if tokens is None:
        return None
    if not tokens.is_expired():
        return tokens
    _log.info("cached session expired, refreshing")
    try:
        return refresh(tokens, store=store, key=key)
    except AuthError as e:
        raise AuthError(_messages.refresh_failed(e)) from e


def logout(
    *,
    store: TokenStore | None = None,
    key: str | None = None,
    everything: bool = False,
    revoke: bool = True,
) -> None:
    """Discard the cached session for the API host, or every cached session.

    Args:
        store: Where tokens are cached.
        key: Cache key; defaults to the API host.
        everything: Forget every host's session, not just this one's.
        revoke: Also ask the provider to invalidate the refresh token, so the
            session cannot be resumed from a copy of the credentials file.
            Best effort: logging out succeeds even if the provider refuses.
    """
    store = store or TokenStore()
    keys = store.keys() if everything else [key or store_key()]
    if revoke:
        for cached in keys:
            tokens = store.load(cached)
            if tokens is not None and tokens.refresh_token:
                _revoke(tokens.refresh_token)
    store.clear(None if everything else keys[0])
