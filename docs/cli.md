# CLI reference

```
audioshake [GLOBAL OPTIONS] COMMAND [ARGS]
```

Logs go to stderr, results to stdout.

## Credentials

The first credential found is used:

1. `--api-key KEY`
2. `AUDIOSHAKE_API_KEY`
3. The session cached by `audioshake login`

Without any of them, commands that call the API exit 3 with a hint.

`AUDIOSHAKE_BASE_URL` may point at another API host. A plain `http://` URL for
anything but loopback logs a warning: the credential would go out in the clear.

## Global options

| Option | Effect |
| --- | --- |
| `--api-key KEY` | API key; otherwise `AUDIOSHAKE_API_KEY`, then the `login` session |
| `--json` | Machine-readable results on stdout |
| `--plain` | Log lines instead of the live progress tree |
| `-v`, `-vv` | INFO, then DEBUG (including HTTP requests) on stderr |
| `-q` | Errors only |
| `--version`, `-h` | |

Global options go before the command: `audioshake --json models`.

`--json` is a **verbatim passthrough** of what the API sent (`tasks list`,
`tasks get` and `models` print the raw objects), so scripts see every field,
including ones this reference does not document. It is not filtered or
redacted: `link` values are presigned URLs that grant anyone who holds them
read access until they expire, so treat `--json` output as credential-bearing
and keep it out of shared logs. Human-readable output carries no links.

## `run`

```
audioshake run SOURCE... -m MODEL[,MODEL...] [options]
```

`SOURCE` is a file, a directory, a public `http(s)` URL, or `asset:ID` for a
file an earlier run uploaded (its `asset_id` in `--json`, or the id a failure
line names). Several may be given. URLs are passed to the API without
uploading, and an asset is used as it is, so more models can run on one file
without uploading it again. An id the API no longer has, typically an upload
that has expired, is a usage error (exit 2) before anything runs.

| Option | Default | Effect |
| --- | --- | --- |
| `-m, --model MODEL[,MODEL]` | required | Models; repeatable |
| `-o, --output DIR` | `audioshake-output` | One sub-directory per source is created inside |
| `-f, --format FMT[,FMT]` | model default | Output formats (`wav`, `mp3`, `flac`, `aiff`, `mp4`; `json`, `txt`, `srt` for text models); each model produces the ones it offers |
| `--residual` | off | Also return everything each model did not capture |
| `--language CODE` | | For models that accept one (transcription, alignment); validated per model |
| `--num-speakers N` | | For `multi_voice` |
| `-c, --concurrency N` | 10 | Sources processed at once. Each source also fetches its outputs in parallel |
| `-r, --recursive` | off | Descend into sub-directories |
| `--no-download` | off | Leave outputs in the API's storage |
| `--timeout SECONDS` | 3600 | Per-task wait |
| `--retries N` | 3 | Request retries; `0` fails on the first error |

### Output layout

One folder per source, named after the source file (for an `asset:ID` source,
the file it was uploaded from); one file per output, named after the output.
This is the layout Demucs and Spleeter use.

```
audioshake-output/
└── song/
    ├── vocals.wav
    ├── drums.wav
    ├── vocals_residual.wav      # with --residual
    ├── drums_residual.wav       # with --residual
    └── transcription.json
```

A residual is named after its model, `<model>_residual.<format>`, whether one
model runs or several. The API chooses output names; read them from `--json`.

Two sources with the same stem in one run (`a/take.wav` and `b/take.wav`, or two
URLs both ending in `sourcefile`) get `take/` and `take-2/`, in input order, so
nothing is overwritten silently. A suffix never reuses a name already given out,
so a source really named `take-2.wav` then gets `take-2-2/`. Names are compared
ignoring case, because `Take/` and `take/` are one folder on macOS and Windows.

When two models produce an output with the same name, the model id is added
(`vocals_residual.wav`, `drums_residual.wav`). Should two outputs
still map to one file name, the later one gets `-2` rather than replacing it.

### Progress

In a terminal the run paints a live view. The header says what is running
(models, output formats, language, speakers, residual), how many files run at a
time when there are several, and where the outputs go. Below it is
one row per source, with the stage it is at (uploading with a percentage, the
task id once it exists, queued, processing, downloading) and how long it has
taken; a source running several models gets one nested row per model. A settled
row keeps its task id, so a finished run can be traced back with `audioshake
tasks get` without re-reading `--json`. A single file keeps its model rows once
it finishes; in a batch they fold away to make room. Two sources that share a
file name show their folder as well. The footer counts files, credits and
elapsed time, with a bar when there is more than one file.

With `--plain`, `--json`, `-v`, or when stdout is not a terminal, one line per
finished source is printed instead:

```
✔ song.wav  task_0123456789abcdef  2.00 credits  4 files
```

Either way the run ends with a summary: where the outputs were written and how
many files, and for a batch the counts, total cost and elapsed time, followed by
any partly completed or failed sources with their task id and reason.

### `--json` output

One JSON object per source, as it finishes (NDJSON):

```json
{"source": "song.wav", "task_id": "cmt...", "asset_id": "cma...", "status": "completed",
 "cost": 2.0, "files": ["audioshake-output/song/vocals.wav"], "error": null}
```

`source` is the SOURCE argument exactly as given (`asset:ID` for an asset), so
two files that share a name (`a/song.wav` and `b/song.wav`) stay apart.

`asset_id` names the upload, which outlives the run; it is `null` for a URL
source, which is never uploaded, and for a failure during the upload. A source
that was uploaded and then failed, say on a model the API rejected, still
carries it, and the on-screen failure line says `uploaded as asset <id>; re-run
with asset:<id> to skip the upload`.

`status` is `completed`, `partial` or `failed`. A `partial` source is one where
some models failed and the rest completed: their files are downloaded and listed
in `files`, and `error` names the models that failed and why. A failed model's
credits are released; the completed ones are charged, and `cost` says so.

A failed source still reports `task_id` and `cost` whenever the task was created
before the failure, whether the wait timed out, a download failed, or every
model did. `cost` is `null`, never `0.0`, when the charge is not known: the run
failed before there was a task, or the task was still running when the run gave
up, as after `--timeout`. Such a task keeps running and is billed as it
completes; `audioshake tasks get TASK_ID` shows the final cost.

### Validation

`--format` and `--language` are checked against `GET /models` before anything is
uploaded; an unsupported value exits 2 with the valid choices. Duplicate models
are rejected because each would be billed.

Model ids themselves are the API's to judge: a model can be callable before it
is listed, so an id the catalog does not know is sent as given. The typos
caught first are the certain ones: an id that differs from a listed one only in
case, hyphens or spaces, that has a listed id's words in another order, or that
has one of them in the singular. `-m multi-voice` exits 2 with
`Did you mean 'multi_voice'?`, and `-m lead-vocal` with
`Did you mean 'vocals_lead'?`, before the upload rather than after it.

`--format` takes one format or a comma list. Every selected model produces the
requested formats its `outputFormats` list, and a model that offers none of them
keeps its own default, so one run can mix kinds:

```
audioshake run song.wav -m alignment --language en -f srt   # alignment.srt
audioshake run song.wav -m vocals,alignment -f srt,mp3      # vocals.mp3 + alignment.srt
audioshake run song.wav -m vocals,alignment -f mp3          # vocals.mp3 + alignment.json
audioshake run song.wav -m alignment -f json,srt            # both, from one billed target
audioshake run song.wav -m vocals -f srt                    # exit 2: vocals cannot output srt
```

Every requested format must reach at least one selected model; one that reaches
none exits 2 with the formats each model offers. A model that keeps its own
default because the format is not one of its own says so at `-v`:

```
alignment: json (does not output mp3; offers json, txt, srt)
```

`alignment` offers `json`, `txt` and `srt`; `transcription` offers `json` and
`txt`. Read `audioshake models` for the current lists rather than assuming.

## `models`

```
audioshake models [--all]
```

Table of the models this account can run: category, credits per minute, output
formats, options (language support, maximum input length) and access. `--all`
includes models that need an access request. `--json` prints the raw API objects.

## `tasks`

```
audioshake tasks list [--limit N] [--since WHEN]
audioshake tasks get TASK_ID
audioshake tasks download TASK_ID [-o DIR]
```

`list` shows the newest 20 tasks. `--limit N` shows up to N instead, fetching
as many pages as that takes. `--since` stops at the first task created before
it, and accepts a duration (`30m`, `24h`, `7d`, `2w`) or an ISO date or
date-time. The two combine: `--limit 500 --since 7d` is at most 500 tasks from
the last week. A task whose creation time the API does not give readably cannot
be placed against the cutoff, so `--since` leaves it out and names it in a
warning on stderr; without `--since` it is listed like any other, with an empty
Created column. `get` shows the task's status and cost, the asset it was made
from as `asset <id>  (<original filename>)` (the id alone when the asset can no
longer be read, nothing for a task made from a URL), then one row per model.
That asset id is what `run asset:ID` takes to run more models on the same
upload. `download` writes `DIR/<original filename stem>/<output>.<format>` for every
completed model of a finished task, the same layout `run` produces; the task id
is the folder name only if the original asset cannot be looked up. `--json` prints raw task objects (`list`, `get`) or the
written paths (`download`).

## `login`, `logout`, `whoami`

```
audioshake login [--no-browser] [--timeout SECONDS]
audioshake logout [--all]
audioshake whoami
```

`login` runs the OAuth 2.0 authorization-code flow with PKCE against the
AudioShake sign-in page: it starts a loopback server on `127.0.0.1` (ports 51739
to 51748), opens the browser, and caches the resulting session in
`~/.audioshake/credentials.json` with mode 600 (in a directory with mode 700),
keyed by API host. The page
either redirects back with an authorization code, which the CLI exchanges for
tokens without a client secret, or posts the tokens to the loopback server
itself when it completed the sign-in. Either way the CLI checks the `state` it
generated; a post is additionally accepted only from the sign-in page's origin
and only when the id token names this application. Later commands refresh the session silently; when the
refresh token has expired they exit 3 and ask you to sign in again.
`--no-browser` prints the URL for machines without a browser. The URL goes to
stderr, with the other messages meant for a person, so stdout keeps only the
result and `audioshake --json login` stays machine-readable. Otherwise the URL
is printed only with `-v`: it carries the flow's `state`, which is what stops
another local process from completing the sign-in, so it stays out of
scrollback and CI logs by default.

`whoami` shows the signed-in identity (from the token's `email` claim), when the
session expires, and whether an API key is taking precedence. Exit 3 when there
is no credential at all, `--json` included: it prints its object with every
field `null` and exits 3 anyway, so a script reads the object and still sees the
failure in the status. `logout` asks the sign-in service to revoke the refresh
token and then deletes the cached session for the current API host, or for
every host with `--all`. Revocation is best effort: an unreachable service, or
one with revocation turned off, is reported at `-v` and does not stop the local
session being forgotten (the tokens then stay valid until they expire).

`AUDIOSHAKE_CONFIG_DIR` moves the credentials file (default `~/.audioshake`).
Other deployments can point sign-in elsewhere with `AUDIOSHAKE_SIGNIN_DOMAIN`
and `AUDIOSHAKE_SIGNIN_CLIENT_ID`, alongside `AUDIOSHAKE_BASE_URL`. That domain
serves both halves of the flow; `AUDIOSHAKE_SIGNIN_TOKEN_DOMAIN` splits the token
exchange onto its own host, as the default deployment does.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | One or more sources failed or only partly completed (see the summary) |
| 2 | Usage error: bad option, missing file, unknown or expired `asset:ID`, duplicate model, unsupported format or language |
| 3 | Authentication failed, no credential, or the sign-in session could not be refreshed |
| 4 | Network failure after retries |
| 5 | Unexpected error: a bug, or an answer the library could not parse. Re-run with `-vv` for the stack trace |
| 130 | Interrupted (Ctrl-C); see below |

A batch can fail several ways at once. `run` exits with the most actionable of
them: authentication (3) first, since nothing will work until it is fixed, then
the network (4), then a bug (5), then a plain failed source (1).

## Error messages

An error message carries the status and the API's own text (`message`, `error`
or `detail` from a JSON body), collapsed onto one line and capped. A body that
is not JSON — an HTML error page from a proxy, say — is not shown: it is
upstream infrastructure detail, so only `HTTP <status> <reason>` appears, and
the body is logged at `-vv`. A model's own failure reason is shown the same way,
capped for the table and the progress tree; when it is a stack trace, only its
last line is shown, since the frames above it are paths on a machine you cannot
reach. The full text is in `--json` and at `-vv`.
The CLI never prints a Python stack trace; `-vv` does.

## Interrupting a run

Ctrl-C stops a batch from starting anything else: queued sources are dropped,
and a source still uploading stops before its task is created, so neither is
billed. A source whose task already exists has been billed, so the run waits
for it to finish and download, naming those tasks in a warning while it waits
(`-q` hides it). Press Ctrl-C again to leave them.

A source that finishes or fails during that wait is reported like any other:
it gets its `--json` line and summary row, and counts in the cost total, so a
script sees every task the batch reached. The run still exits 130.
A task whose create request was still in flight at the first Ctrl-C is not
named in the warning; `audioshake tasks list --since 1h` shows it.

`tasks download` does not wait for a task, and writes under
`audioshake-output/` unless told otherwise. For a task left running, wait until
`audioshake tasks get TASK_ID` shows it finished, then run
`audioshake tasks download TASK_ID -o DIR` with the run's `-o` so the files land
under the same output root as the rest of the batch.

## Retries

Connection errors, timeouts, `408`, `425`, `429` and `5xx` responses are retried with jittered
exponential backoff, honouring `Retry-After`. A download that fails part-way is
fetched again from the start. Failures that would recur, such as an unsupported
input or a model that failed on the file, are reported once and not retried.

Creating a task is the exception, because a task is billed once it exists. Only
responses where the API declined to act (`408`, `425`, `429`, `503`) are retried.
A lost response or a `500`/`502`/`504` may mean the task was created anyway, so
the CLI stops and warns instead; check `audioshake tasks list` before re-running.
