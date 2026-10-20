# Security policy

Covers the `audioshake` library and the CLI built on it. Report server-side
issues in the AudioShake API to the same address and we'll pass them to the
people who run it.
Only the latest release is supported.

## Reporting

Email **security@audioshake.ai**. Please don't open a public issue or PR for a
suspected vulnerability, and give us time to ship a fix before disclosing.

Tell us what an attacker gains, the version (`audioshake --version`) and OS,
and how to reproduce it. We'll acknowledge your report and keep you posted as
we work on a fix. Report in good faith and we'll credit you if you want it and
won't pursue legal action.

## In scope

The CLI signs a user in, caches the session on disk, uploads audio, downloads
results, and talks to the API over HTTPS. The boundary is the credential on
the local machine, the sign-in flow, and the files the CLI reads and writes.

- Credentials — API keys, tokens, presigned links — reaching logs, error
  messages, `--json`, or a world-readable file.
- Flaws in sign-in: PKCE, the `state` check, the loopback callback, or how
  cached tokens are refreshed and reused.
- Code execution or injection from a malicious input file, API response, or
  presigned link.
- A name from an API response that escapes the output directory.
- Default permissions on `~/.audioshake/credentials.json` and its directory
  (`0600` and `0700`; `AUDIOSHAKE_CONFIG_DIR` moves both).
- Certificate verification skipped or downgraded against an `https` base URL.

## Known and accepted

- **Tokens are stored unencrypted**, protected by file permissions rather than
  an OS keychain.
- **`AUDIOSHAKE_BASE_URL` may be plain http.** The CLI warns that credentials
  will travel in the clear and proceeds, so a local proxy works; `-q` silences
  the warning. The default is HTTPS and verification is never disabled.

## Out of scope

- The runtime, OS, terminal, or hosting infrastructure.
- Anything needing already-stolen credentials, or a user talked into running
  an untrusted command.
- User code or pipelines that call the library or CLI.
- Dependencies, unless reachable through a library or CLI code path. Reachable
  ones — `certifi`, `httpcore`, `h11`/`h2`, `anyio`, `idna` via `httpx` — get
  a version floor bump at minimum.
