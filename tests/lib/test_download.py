import httpx
import pytest

from audioshake import APIConnectionError, AuthenticationError, NotFoundError
from audioshake._download import async_download, download


@pytest.fixture
def transfer():
    with httpx.Client() as c:
        yield c


def test_streams_to_file_and_reports_progress(storage, transfer, tmp_path):
    body = b"a" * (1024 * 1024 + 10)
    storage.get("/big.wav").respond(200, content=body, headers={"content-length": str(len(body))})
    seen = []
    dest = download(
        transfer,
        "https://storage.test/big.wav",
        tmp_path / "out" / "big.wav",
        on_progress=lambda w, t: seen.append((w, t)),
    )
    assert dest.read_bytes() == body
    assert seen[-1] == (len(body), len(body))
    assert all(t == len(body) for _, t in seen)
    assert not (tmp_path / "out" / "big.wav.part").exists()


def test_retries_whole_transfer_then_succeeds(storage, transfer, tmp_path, no_sleep):
    route = storage.get("/x.wav")
    route.side_effect = [httpx.Response(503), httpx.Response(200, content=b"ok")]
    dest = download(transfer, "https://storage.test/x.wav", tmp_path / "x.wav", retries=1)
    assert dest.read_bytes() == b"ok"
    assert route.call_count == 2 and len(no_sleep) == 1


def test_retry_log_does_not_include_the_presigned_query(
    storage, transfer, tmp_path, no_sleep, caplog
):
    route = storage.get("/x.wav")
    route.side_effect = [httpx.Response(503), httpx.Response(200, content=b"ok")]
    with caplog.at_level("WARNING", logger="audioshake.http"):
        download(
            transfer,
            "https://storage.test/x.wav?X-Amz-Signature=SECRET",
            tmp_path / "x.wav",
            retries=1,
        )
    assert "storage.test/x.wav" in caplog.text and "SECRET" not in caplog.text


def test_permanent_status_is_not_retried(storage, transfer, tmp_path, no_sleep):
    route = storage.get("/gone.wav").respond(404)
    with pytest.raises(NotFoundError):
        download(transfer, "https://storage.test/gone.wav", tmp_path / "g.wav")
    assert route.call_count == 1 and no_sleep == []


def _drops_after_first_chunk():
    yield b"half a file"
    raise httpx.ReadError("connection reset")


async def _async_drops_after_first_chunk():
    yield b"half a file"
    raise httpx.ReadError("connection reset")


def _ctrl_c(written, total):
    raise KeyboardInterrupt


@pytest.mark.parametrize(
    ("responses", "kwargs", "raises"),
    [
        (lambda: httpx.ConnectError("down"), {"retries": 1}, APIConnectionError),
        (
            lambda: [httpx.Response(200, content=_drops_after_first_chunk()), httpx.Response(403)],
            {"retries": 1},
            AuthenticationError,
        ),
        (
            lambda: [httpx.Response(200, content=b"a" * 10)],
            {"on_progress": _ctrl_c},
            KeyboardInterrupt,
        ),
    ],
    ids=["connection error", "permanent status after a partial attempt", "interrupt mid-transfer"],
)
def test_no_partial_file_survives_a_failed_transfer(
    storage, transfer, tmp_path, responses, kwargs, raises
):
    """Whatever ends a download short of the rename, the .part file does not stay behind."""
    # A bare exception repeats on every attempt; a list plays out one response per attempt.
    storage.get("/x.wav").mock(side_effect=responses())
    with pytest.raises(raises):
        download(transfer, "https://storage.test/x.wav", tmp_path / "x.wav", **kwargs)
    assert list(tmp_path.iterdir()) == []


async def test_async_permanent_status_after_a_partial_attempt_leaves_no_partial_file(
    storage, tmp_path, monkeypatch
):
    async def no_wait(delay):
        return None

    monkeypatch.setattr("audioshake._retry.async_sleep", no_wait)
    storage.get("/x.wav").side_effect = [
        httpx.Response(200, content=_async_drops_after_first_chunk()),
        httpx.Response(403),
    ]
    async with httpx.AsyncClient() as client:
        with pytest.raises(AuthenticationError):
            await async_download(
                client, "https://storage.test/x.wav", tmp_path / "x.wav", retries=1
            )
    assert list(tmp_path.iterdir()) == []
