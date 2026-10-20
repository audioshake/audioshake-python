# Library reference

```python
from audioshake import AudioShake, AsyncAudioShake, Target
```

## Clients

```python
AudioShake(api_key=None, *, bearer_token=None, base_url=None, timeout=30.0, retries=3,
           download_concurrency=8, http_client=None)
AsyncAudioShake(...)   # same arguments; http_client is an httpx.AsyncClient
```

| Argument | Default | Notes |
| --- | --- | --- |
| `api_key` | `AUDIOSHAKE_API_KEY` | Sent as `x-api-key`. Wins over `bearer_token` |
| `bearer_token` | | A token from `audioshake.auth.login()`, sent as `Authorization: Bearer`. `AuthenticationError` at construction if no credential is available |
| `base_url` | `AUDIOSHAKE_BASE_URL`, else the public API | |
| `timeout` | 30 s read, 10 s connect | A float, an `httpx.Timeout`, or `None` for no timeout; transfers use a 300 s read timeout |
| `retries` | 3 | Retries after the first attempt for connection errors, timeouts, 408/425/429/5xx. `tasks.create()` retries only 408/425/429/503, since any other failure may follow a billed task |
| `download_concurrency` | 8 | Outputs fetched at once. One budget shared by every download the client makes |
| `http_client` | | Bring your own `httpx.Client` (proxy, custom transport); auth headers are applied to it |

Both clients are context managers. Call `close()` (or `await close()`) otherwise.

## `run()`

```python
client.run(source, models, *, output_dir=None, format=None, residual=False,
           language=None, num_speakers=None, download=True,
           poll_interval=10.0, timeout=3600.0, on_progress=None) -> RunResult
```

Upload a file, pass a URL through, or reuse an `Asset` from an earlier upload
(`assets.upload()`, or a previous result's `.asset`), then create, wait and
download. `models` is one model id, comma list or `Target`, or any
iterable of them: `"vocals"`, `"vocals,drums"` and
`["vocals", Target("alignment", formats=("srt",))]` all work. Per-model defaults come from the model
catalogue: audio models get `wav`, text models `json`, `language` applies only
where supported, `num_speakers` only to `multi_voice`. `format` is one format, a
comma list or a sequence, and each model produces those it offers — `"srt,mp3"`
gives `alignment` its SRT and the stems their MP3, `("json", "srt")` gives
`alignment` both files from one billed target — while a model offering none of
them keeps its default. A format no selected model offers, or an unsupported
`language`, raises `ValidationError` before upload. `Target` still gives full
per-model control:

```python
client.run("song.wav", ["vocals", Target("alignment", formats=("srt",), language="en")])
```

Files are written to `output_dir` (default: current directory) as
`{output}.{format}`, e.g. `vocals.wav`; give each source its own directory.
`RunResult` has `.task`, `.files`, `.failed` and `.asset`. `.files` and
`.failed` are tuples, like every sequence on these frozen results, so a result
cannot be changed after it is returned; `list(result.files)` gives a list.
`.asset` is the uploaded source (`None` for a URL): assets outlive the run, so
more models can be run on `result.asset` later without uploading the file again. When some models fail and others
complete, `run()` still downloads the completed ones and returns; `.failed` names
the targets that did not finish, so check it before assuming every model ran.
It raises `TaskFailedError` only when every target failed. `tasks.wait()` is
stricter and raises on any failed target.

`on_progress` is a `ProgressHandler`: it receives
`ProgressEvent(stage, detail, done, total, task, asset)` and its return value is
ignored
with `stage` in `upload`, `queued`, `processing`, `download`, `done`. The
`upload` event that completes the upload carries the `asset`, so a caller
learns its id even if creating the task then fails.

## Resources

### `client.assets`

```python
assets.upload(path, *, on_progress=None) -> Asset     # on_progress(bytes_sent, total)
assets.get(asset_id) -> Asset                          # name and format of an uploaded file
```

### `client.tasks`

```python
tasks.create(source, targets) -> Task
tasks.get(task_id) -> Task
tasks.list(*, limit=None, since=None) -> list[Task]
tasks.wait(task_id, *, poll_interval=10.0, timeout=3600.0, on_poll=None) -> Task
tasks.download(task, output_dir, *, name_prefix="", on_progress=None) -> list[Path]
```

`get()` and `wait()` take a task id or a `Task`. `create()` takes any iterable of
`Target`s. Every path argument in the library is a `StrPath`: a string or any
path-like object, as `pathlib.Path` accepts. Every callback may return a value;
the library ignores it.

`list()` returns tasks newest first, paging through the API as needed: every
task by default, at most `limit` of them, or those created at or after `since`
(a `datetime`; naive means UTC).

`source` is an `Asset`, an asset id, or an `http(s)` URL. `wait()` raises
`TaskFailedError` (with `.task`) if any target failed and `TaskTimeoutError` on
timeout. `download()` skips targets that did not complete and disambiguates
outputs that share a name across models by prefixing the model id.

`download()` fetches a task's outputs concurrently, which matters for models
that produce many of them — `multi_voice` returns one output per speaker. The
paths come back in API order however the transfers interleave, and a failure is
raised only once the other transfers have settled, so a failed download never
strands a partial file. `download_concurrency` is a single per-client budget, so
code that already runs several sources in parallel keeps a fixed ceiling on open
connections rather than multiplying the two levels together.

### `client.models`

```python
models.list(*, enabled_only=True) -> list[Model]      # GET /models, cached per client
models.get(model_id) -> Model                          # ValidationError if unknown
models.refresh()
```

## Types

Aliases for annotating your own code, all importable from `audioshake`:

```python
StrPath = str | os.PathLike[str]  # any path argument
ProgressStage = Literal["upload", "queued", "processing", "download", "done"]
ProgressHandler = Callable[[ProgressEvent], object]  # run(on_progress=...)
ProgressCallback = Callable[[int, int], object]  # assets.upload(on_progress=...)
```

All are frozen dataclasses, and their sequences are tuples. Response types keep
the full API object in `.raw`.

```python
Target(model, formats=("wav",), residual=False, language=None, num_speakers=None)
Output(name, format, link)
TargetResult(
    model, status, cost, duration, error, outputs
)  # status: pending|processing|completed|failed
Task(id, targets, raw)  # .done .failed .succeeded .cost .outputs .created_at
Asset(id, name, format, link)
Model(
    id,
    name,
    category,
    description,
    credits_per_minute,
    output_formats,
    input_formats,
    supported_languages,
    max_input_seconds,
    access,
)
# .enabled .default_format .produces_media
RunResult(task, files, asset)  # .failed
ProgressEvent(stage, detail, done, total, task, asset)
```

## Exceptions

```
AudioShakeError
├── APIError(status, body, request_id, full_message)
│   ├── AuthenticationError   401 / 403
│   ├── NotFoundError         404
│   ├── RateLimitError        429 (retried first)
│   └── ServerError           5xx (retried first)
├── APIConnectionError
│   └── APITimeoutError
├── UnexpectedResponseError(body)  a success status with a body that cannot be read
├── AuthError                 browser sign-in / token refresh
├── TaskFailedError(task)
├── TaskTimeoutError(task_id, timeout)
└── ValidationError
```

An `APIError` message carries the API's error text on one capped line, for
display; `full_message` has it uncapped.

The library raises; it never logs a warning and returns a default.

## Browser sign-in (`audioshake.auth`)

```python
from audioshake import AudioShake, auth

tokens = auth.get_valid_token() or auth.login()  # cached session, else browser sign-in
client = AudioShake(bearer_token=tokens.api_token)
```

| Function | Purpose |
| --- | --- |
| `login(*, open_browser=True, store=None, key=None, timeout=300, on_url=None)` | Authorization code + PKCE flow; caches and returns a `TokenSet` |
| `get_valid_token(*, store=None, key=None)` | Cached `TokenSet` (refreshed if expired), `None` if nothing is cached; `AuthError` if it cannot be refreshed |
| `refresh(tokens, *, store=None, key=None)` | Exchange the refresh token |
| `logout(*, store=None, key=None, everything=False, revoke=True)` | Revoke the refresh token (best effort), then forget the session |
| `get_signin_config()` | Sign-in settings, from `AUDIOSHAKE_SIGNIN_DOMAIN` / `AUDIOSHAKE_SIGNIN_TOKEN_DOMAIN` / `AUDIOSHAKE_SIGNIN_CLIENT_ID` or the defaults |
| `TokenSet` | `.api_token`, `.is_expired()`, `.describe()`, `.claims` |
| `TokenStore(config_dir=None)` | The credentials file; keyed by API host (`store_key()`) |

Errors raise `AuthError` (an `AudioShakeError`).

## Helpers

```python
audioshake.expand_models(["vocals, drums", "bass"])  # ['vocals', 'drums', 'bass']
audioshake.build_targets(models, *, catalog=None, format=None, residual=False,
                         language=None, num_speakers=None) -> list[Target]
# catalog: the list from models.list(), or a {model_id: Model} mapping
audioshake.SPEAKER_COUNT_MODELS
```

## Logging

Everything goes to the `audioshake` logger (`audioshake.http`, `audioshake.tasks`)
with a `NullHandler` installed. DEBUG shows requests and poll ticks, INFO state
changes, WARNING retries.

```python
import logging

logging.basicConfig(level=logging.INFO)
```
