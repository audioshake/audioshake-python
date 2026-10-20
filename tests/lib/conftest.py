import json
from pathlib import Path

import pytest
import respx

from audioshake import AudioShake, _retry

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
BASE_URL = "https://api.test"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Retries and polling must never wait for real in tests."""
    slept: list[float] = []
    monkeypatch.setattr(_retry, "sleep", slept.append)
    return slept


@pytest.fixture
def api():
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def storage():
    """Presigned output links live on a different host with no auth."""
    with respx.mock(base_url="https://storage.test", assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def client(api):
    with AudioShake(api_key="test-key", base_url=BASE_URL, retries=2) as c:
        yield c


@pytest.fixture
def models_json():
    return load_fixture("models.json")


@pytest.fixture
def task_json():
    return load_fixture("task_completed.json")
