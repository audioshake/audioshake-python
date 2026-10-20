"""Global options shared by every subcommand, carried on the click context."""

from __future__ import annotations

import os
from dataclasses import dataclass

from audioshake import AudioShake, AuthenticationError, auth
from audioshake._http import API_KEY_ENV

from . import _messages


@dataclass
class Settings:
    api_key: str | None = None
    json: bool = False
    plain: bool = False
    verbosity: int = 0
    quiet: bool = False

    def client(self, **kwargs: object) -> AudioShake:
        """Build the client with the first credential available.

        Order: ``--api-key``, then ``AUDIOSHAKE_API_KEY``, then the session cached
        by ``audioshake login``.
        """
        if self.api_key or os.environ.get(API_KEY_ENV):
            return AudioShake(self.api_key, **kwargs)  # type: ignore[arg-type]
        tokens = auth.get_valid_token()
        if tokens is None:
            raise AuthenticationError(_messages.not_signed_in(API_KEY_ENV), status=401)
        return AudioShake(bearer_token=tokens.api_token, **kwargs)  # type: ignore[arg-type]


# Exit codes. Click uses 2 for usage errors on its own.
EXIT_OK = 0
EXIT_FAILED_ITEMS = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_NETWORK = 4
# A bug in the CLI or an answer the library could not parse: not the caller's
# fault and not retryable, so it must not look like a failed item (1).
EXIT_UNEXPECTED = 5
