# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/). While the major version is 0 the
public surface may still change between minor releases.

## [0.1.0] - Unreleased

Initial public release of the `audioshake` Python library and CLI for the
[AudioShake API](https://developer.audioshake.ai). Python 3.10 or later.

### Library
- `AudioShake` and `AsyncAudioShake` clients with the same surface:
  `assets.upload/get`, `tasks.create/get/list/wait/download` and
  `models.list/get`. The model catalogue comes from the API, not the code.
- `run()`: upload a file, pass a URL, or reuse an earlier upload; create the
  task, wait, and download the outputs in one call, with a progress callback.
  Formats and languages are checked against the catalogue before anything is
  uploaded, and a task where some models failed still returns the ones that
  completed.
- Typed, frozen results that keep the full API object in `.raw`, and a typed
  exception hierarchy under `AudioShakeError`.
- Retries with backoff for transient failures. Task creation is retried only
  when the API declined to act, since a created task is billed.
- Browser sign-in (`audioshake.auth`) with OAuth 2.0 authorization code and
  PKCE, or an API key.
- Logs to the `audioshake` logger, never prints, and ships `py.typed`.

### CLI
- `audioshake run SOURCE... -m MODEL[,MODEL]` for files, directories, URLs and
  earlier uploads, with a live progress view in a terminal. Outputs land in
  `OUT/<source>/<output>.<format>`, the layout Demucs and Spleeter use.
- `audioshake models`, `audioshake tasks list|get|download`, and
  `audioshake login|whoami|logout`.
- Logs on stderr, results on stdout, `--json` for scripts. Exit codes: 0
  success, 1 a source failed, 2 usage, 3 authentication, 4 network,
  5 unexpected error, 130 interrupted.

### Also in this repository
- A Claude Code plugin (`plugin/`) that teaches Claude to use the CLI and
  library.

[0.1.0]: https://github.com/audioshake/audioshake-python/releases/tag/v0.1.0
