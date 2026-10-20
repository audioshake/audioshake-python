---
name: audioshake
description: Use the `audioshake` CLI and Python library to run AudioShake models on audio or video files: stem separation (vocals, drums, bass, guitar, piano...), dialogue and effects isolation, speech denoise/dereverb, multi-speaker separation, music detection/removal, lyric transcription and alignment. Trigger when the user wants stems, an instrumental, an a cappella, isolated dialogue, speaker separation, lyrics or word timings from media, or asks about AudioShake tasks, models or credits.
---

# AudioShake CLI

`audioshake` runs models from AudioShake's API on files, directories or public
URLs and downloads the results. Every run costs credits, charged per minute of
audio per model, so confirm scope with the user before large batches.

## Setup check

```bash
audioshake --version || uv tool install audioshake   # or: pipx install audioshake
audioshake whoami        # which credential is active; exit 3 if none (--json too)
```

Credentials, first found wins: `--api-key`, then `AUDIOSHAKE_API_KEY`, then the
session cached by `audioshake login` (browser sign-in; `--no-browser` prints the
URL instead, `--timeout` bounds the wait). `audioshake logout` revokes it and
forgets it (`--all` for every API host). Never print or log a key. On exit 3, ask the user
for a key or to run `audioshake login`; do not guess.

## Discover models before choosing them

```bash
audioshake --json models | jq -r '.[] | [.id, .creditsPerMinute, (.outputFormats|join(","))] | @tsv'
audioshake models --all      # include models the account must request access to
```

Model ids are the source of truth from the API; do not invent ids. Common ones:

| Want | Models |
| --- | --- |
| Stems for remixing | `vocals,drums,bass,other` or add `guitar`, `piano`, `keys`, `strings`, `wind` |
| Instrumental / a cappella | `instrumental` / `vocals` |
| Lead vs backing vocals | `vocals_backing,vocals_lead` |
| Film/TV: dialogue, effects, music | `effects,dialogue,music_fx` |
| Clean up speech | `speech_denoise`, `speech_dereverb`, `dialogue` |
| Separate speakers | `multi_voice --num-speakers N` |
| Remove or detect music | `music_removal`, `music_detection`, `music_identification` |
| Lyrics | `transcription --language en` (`json`, `txt`), `alignment --language en` (word timings; `json`, `txt`, `srt`) |

Text models (`transcription`, `alignment`, `music_detection`,
`music_identification`) default to JSON; `-f txt` or `-f srt` works where the
model lists it, and a comma list covers a mixed run (`-m vocals,alignment
-f srt,mp3` -> `vocals.mp3` + `alignment.srt`). Several formats for one model
(`-m alignment -f json,srt`) come from one billed target. A model may appear
once per run.

## Run

```bash
audioshake run SOURCE... -m MODEL[,MODEL]... [-o DIR] [options]

audioshake run song.wav -m vocals,drums -o stems/
audioshake run ./album -m effects,dialogue,music_fx -f mp3 --residual   # directory; -r to recurse
audioshake run https://example.com/talk.mp3 -m transcription --language en
audioshake run song.wav -m alignment --language en -f srt   # subtitles
audioshake run song.wav -m vocals,alignment -f srt,mp3      # per-model formats
audioshake run panel.wav -m multi_voice --num-speakers 3
audioshake run asset:ID -m drums,bass     # reuse an earlier upload (asset_id from --json); no re-upload
```

| Option | Meaning |
| --- | --- |
| `-o DIR` | Output root; one sub-directory per source (default `audioshake-output`) |
| `-f FMT[,FMT]` | `wav` (default), `mp3`, `flac`, `aiff`, `mp4`; `json`, `txt`, `srt` for text models. Each model produces the ones it offers, others keep their default; a format no model offers is exit 2 |
| `--residual` | Also return everything the models did not capture |
| `--language CODE` | For transcription/alignment; validated against the model |
| `--num-speakers N` | For `multi_voice` |
| `-c N` | Concurrent sources (default 10) |
| `--no-download` | Leave outputs in the API's storage |
| `--timeout SECONDS` | Per-task wait (default 3600); raise it for very long inputs |
| `--retries N` | Request retries (default 3); `0` fails on the first error |

Output layout (same as Demucs/Spleeter): `DIR/<source stem>/<output>.<format>`,
for example `stems/song/vocals.wav`, `stems/song/drums.wav`. With `--residual`
each model adds `<model>_residual.<format>`, for example `vocals_residual.wav`. Sources sharing a stem
in one run get `<stem>-2/`, `<stem>-3/`; always read paths from `--json` output.

## Machine-readable mode (prefer this from an agent)

Logs go to stderr, results to stdout. `--json` goes **before** the subcommand:

```bash
audioshake --json run song.wav -m vocals,drums -o stems/
# one JSON object per source (NDJSON):
# {"source":"song.wav","task_id":"...","asset_id":"...","status":"completed",
#  "cost":2.0,"files":["stems/song/vocals.wav", ...],"error":null}
# `source` is the argument as given. asset_id names the upload (null for a URL
# source or a failure during the upload). status is completed, partial or failed.
# partial: some models failed, the rest were downloaded (their paths are in
# files; error names the failed models). A failed source still carries task_id
# and cost when the task existed. cost is null (not 0.0) when it is not known:
# no task, or a task still running after --timeout (check `tasks get` later).
audioshake --json models
audioshake --json tasks list
audioshake --json tasks get TASK_ID
audioshake --json tasks download TASK_ID -o out/
```

Read `.files` from the output rather than guessing paths. Use `--plain` for
readable logs without the live tree, `-v` for progress lines, `-q` for errors only.

`tasks get`, `tasks list` and `models` print the API's objects verbatim under
`--json`, unfiltered: fields beyond the ones documented here may appear, and
`output[].link` values are presigned URLs that anyone holding them can download
until they expire. Read what you need and do not echo whole `--json` objects
back to the user or into a log; download with `tasks download`, not by fetching
links yourself.

## Exit codes

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Success | Report files and cost |
| 1 | One or more sources failed or partly completed | Show the summary; report the files that were written and each failed model's reason. Don't re-run the whole source: the completed models are already on disk and charged |
| 2 | Usage: bad option, missing file, duplicate model, unsupported format/language | Fix the command; the message names the valid choices |
| 3 | No credential or authentication failed | Ask the user for an API key or to run `audioshake login` |
| 4 | Network failure after retries | Retry later; don't loop |
| 5 | Unexpected error (a bug, or an unparseable answer) | Don't retry; re-run with `-vv` and report the stack trace |
| 130 | Interrupted (Ctrl-C) | Queued sources were not started or billed; tasks already created were billed. Sources that finished or failed while the first Ctrl-C waited are reported like any other (a `--json` line, a summary row, counted in the cost), and their files are in the output directory. The "waiting for N task(s)" log line names the tasks (hidden by `-q`); `audioshake tasks list --since 1h` finds any it missed, such as one created as Ctrl-C landed. For a task left by a second Ctrl-C, wait until `audioshake tasks get TASK_ID` shows it finished, then run `audioshake tasks download TASK_ID -o DIR` with the run's `-o`. Don't re-run the source |

When a batch fails several ways at once, the code is the most actionable one:
3 before 4 before 5 before 1. Read the per-source `--json` lines for the rest.

Retries for transient errors are automatic; do not wrap the CLI in your own
retry loop. A failed model is reported once and is not re-billed. If the log says
a task "may have been created and billed", run `audioshake tasks list` before
re-running, or the same work is paid for twice.

## Inspect and recover

```bash
audioshake tasks list --limit 50 --since 24h   # newest first (default 20); durations or ISO dates
audioshake tasks get TASK_ID          # per-model status, outputs, errors, and the asset id
audioshake tasks download TASK_ID -o out/   # fetch a finished task again
```

Outputs stay downloadable from the API for a while after a run, so a
`--no-download` run or a lost download can be recovered by task id. `tasks get`
also shows the asset the task was made from; `audioshake run asset:ID -m ...`
runs more models on that upload without uploading again, while it is still
stored.

An upload outlives its run too: every `--json` line carries `asset_id`, and a
failure after the upload prints it. `audioshake run asset:ID -m ...` runs more
models on that file, or retries a failed run, without uploading it again. An id
the API no longer has (uploads expire after a few days) is exit 2 before
anything runs; upload the file again instead.

## Python library

Same capabilities, for scripts:

```python
from audioshake import AudioShake, Target

with AudioShake() as client:  # reads AUDIOSHAKE_API_KEY
    result = client.run("song.wav", ["vocals", "drums"], output_dir="stems/song")
    print(result.task.cost, [str(f) for f in result.files])

    # Step by step, or from asyncio with AsyncAudioShake:
    asset = client.assets.upload("interview.mp3")
    task = client.tasks.create(asset, [Target("multi_voice", num_speakers=2)])
    task = client.tasks.wait(task.id)
    client.tasks.download(task, "out/")
```

Exceptions are typed (`TaskFailedError` carries `.task`; `AuthenticationError`,
`RateLimitError`, `ValidationError`...). The library retries transient failures
itself and logs to the `audioshake` logger.

## Gotchas

- Directories are not recursive by default; add `-r`.
- Error text is capped for display; `-vv` has the API's full reason and the
  request bodies. Never paste `-vv` output anywhere shared: it includes
  presigned download URLs.
- Global flags (`--json`, `--plain`, `--api-key`, `-v`) go before the subcommand.
- `-m vocals,vocals` is rejected: a model runs once per source.
- Model ids use underscores, not hyphens, their word order matters, and they
  are plural (`vocals`, `drums`, `keys`). `-m multi-voice` exits 2 before the
  upload with `Did you mean 'multi_voice'?`, and `-m lead-vocal` with
  `Did you mean 'vocals_lead'?`; use the suggested id.
  An id the catalog does not list (a pre-release model) is still sent to the API.
- Long inputs: `multi_voice` accepts up to 90 minutes, transcription/alignment
  45 minutes; `audioshake models` shows limits.
- Inputs: wav, mp3, flac, aiff, aac, m4a, mp4, mov. Anything else is rejected
  by the API with the reason in the failure summary.
