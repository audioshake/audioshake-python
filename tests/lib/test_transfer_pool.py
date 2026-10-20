"""Concurrency, ordering and failure behaviour of the shared transfer pool."""

import asyncio
import threading

import httpx
import pytest

from audioshake import NotFoundError
from audioshake._download import AsyncTransferPool, TransferJob, TransferPool


@pytest.fixture
def transfer():
    with httpx.Client() as c:
        yield c


@pytest.fixture
async def async_transfer():
    async with httpx.AsyncClient() as c:
        yield c


def jobs_for(tmp_path, count, on_progress=None):
    return [
        TransferJob(f"https://storage.test/{i}.wav", tmp_path / f"{i}.wav", on_progress)
        for i in range(count)
    ]


class TestSyncPool:
    def test_transfers_overlap_up_to_the_budget(self, storage, transfer, tmp_path):
        # Three threads must meet at the barrier for it to release, so this
        # cannot pass unless transfers genuinely run at the same time.
        barrier = threading.Barrier(3, timeout=10)
        live = 0
        peak = 0
        lock = threading.Lock()

        def handler(request):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            barrier.wait()
            with lock:
                live -= 1
            return httpx.Response(200, content=b"pcm")

        storage.get(path__regex=r"/\d+\.wav").mock(side_effect=handler)
        paths = TransferPool(3).run(transfer, jobs_for(tmp_path, 6))

        assert peak == 3
        assert len(paths) == 6

    def test_results_keep_job_order_however_transfers_interleave(self, storage, transfer, tmp_path):
        # Later jobs return first; the returned paths must not follow completion order.
        def handler(request):
            index = int(request.url.path.strip("/").split(".")[0])
            return httpx.Response(200, content=str(index).encode())

        storage.get(path__regex=r"/\d+\.wav").mock(side_effect=handler)
        paths = TransferPool(4).run(transfer, jobs_for(tmp_path, 6))
        assert [p.name for p in paths] == [f"{i}.wav" for i in range(6)]

    def test_one_failure_raises_but_lets_the_others_finish(self, storage, transfer, tmp_path):
        def handler(request):
            if request.url.path == "/2.wav":
                return httpx.Response(404)
            return httpx.Response(200, content=b"pcm")

        storage.get(path__regex=r"/\d+\.wav").mock(side_effect=handler)
        with pytest.raises(NotFoundError):
            TransferPool(4).run(transfer, jobs_for(tmp_path, 5))

        # Every other transfer still completed, and nothing left a .part behind.
        written = sorted(p.name for p in tmp_path.iterdir())
        assert written == ["0.wav", "1.wav", "3.wav", "4.wav"]

    def test_budget_is_shared_across_concurrent_download_calls(self, storage, transfer, tmp_path):
        pool = TransferPool(2)
        live = 0
        peak = 0
        lock = threading.Lock()

        def handler(request):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            threading.Event().wait(0.02)
            with lock:
                live -= 1
            return httpx.Response(200, content=b"pcm")

        storage.get(path__regex=r"/.+\.wav").mock(side_effect=handler)

        def call(prefix):
            jobs = [
                TransferJob(f"https://storage.test/{prefix}{i}.wav", tmp_path / f"{prefix}{i}.wav")
                for i in range(4)
            ]
            pool.run(transfer, jobs)

        threads = [threading.Thread(target=call, args=(p,)) for p in ("a", "b", "c")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert peak <= 2  # three callers, one budget
        pool.close()

    @pytest.mark.parametrize("budget", [1, 2])
    def test_concurrent_single_output_calls_share_the_budget(
        self, storage, transfer, tmp_path, budget
    ):
        # One output per call takes the no-thread path; it must still count.
        pool = TransferPool(budget)
        live = 0
        peak = 0
        lock = threading.Lock()

        def handler(request):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            threading.Event().wait(0.05)
            with lock:
                live -= 1
            return httpx.Response(200, content=b"pcm")

        storage.get(path__regex=r"/.+\.wav").mock(side_effect=handler)

        def call(name):
            pool.run(
                transfer,
                [TransferJob(f"https://storage.test/{name}.wav", tmp_path / f"{name}.wav")],
            )

        threads = [threading.Thread(target=call, args=(f"s{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert 1 <= peak <= budget
        assert len(list(tmp_path.iterdir())) == 5
        pool.close()


class TestAsyncPool:
    async def test_transfers_overlap_up_to_the_budget(self, storage, async_transfer, tmp_path):
        live = 0
        peak = 0

        async def handler(request):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1
            return httpx.Response(200, content=b"pcm")

        storage.get(path__regex=r"/\d+\.wav").mock(side_effect=handler)
        paths = await AsyncTransferPool(3).run(async_transfer, jobs_for(tmp_path, 6))

        assert peak == 3
        assert [p.name for p in paths] == [f"{i}.wav" for i in range(6)]

    async def test_one_failure_raises_but_lets_the_others_finish(
        self, storage, async_transfer, tmp_path
    ):
        async def handler(request):
            if request.url.path == "/2.wav":
                return httpx.Response(404)
            return httpx.Response(200, content=b"pcm")

        storage.get(path__regex=r"/\d+\.wav").mock(side_effect=handler)
        with pytest.raises(NotFoundError):
            await AsyncTransferPool(4).run(async_transfer, jobs_for(tmp_path, 5))

        written = sorted(p.name for p in tmp_path.iterdir())
        assert written == ["0.wav", "1.wav", "3.wav", "4.wav"]

    async def test_concurrent_single_output_calls_share_the_budget(
        self, storage, async_transfer, tmp_path
    ):
        pool = AsyncTransferPool(1)
        live = 0
        peak = 0

        async def handler(request):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.02)
            live -= 1
            return httpx.Response(200, content=b"pcm")

        storage.get(path__regex=r"/.+\.wav").mock(side_effect=handler)
        await asyncio.gather(
            *(
                pool.run(
                    async_transfer,
                    [TransferJob(f"https://storage.test/s{i}.wav", tmp_path / f"s{i}.wav")],
                )
                for i in range(5)
            )
        )

        assert peak == 1
        assert len(list(tmp_path.iterdir())) == 5
