import sys
import time
from collections.abc import Callable
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

from app.jobs import dispatcher
from app.main import create_app
from app.settings import get_settings


def _set_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


def _fake_module(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: object) -> None:
    module = ModuleType(name)
    module.__dict__.update(attrs)
    monkeypatch.setitem(sys.modules, name, module)


def test_seeds_in_development_when_enabled(settings_env: object, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    _fake_module(monkeypatch, "app.seed", seed_demo=calls.append)
    _set_env(monkeypatch, APP_ENV="development", SEED_DEMO_DATA="true")
    with TestClient(create_app()):
        pass
    assert len(calls) == 1


@pytest.mark.parametrize(("app_env", "seed"), [("test", "true"), ("development", "false")])
def test_does_not_seed_otherwise(
    settings_env: object, monkeypatch: pytest.MonkeyPatch, app_env: str, seed: str
) -> None:
    calls: list[object] = []
    _fake_module(monkeypatch, "app.seed", seed_demo=calls.append)
    _set_env(monkeypatch, APP_ENV=app_env, SEED_DEMO_DATA=seed)
    with TestClient(create_app()):
        pass
    assert calls == []


def test_embedded_worker_polls_until_shutdown(settings_env: object, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def run_once(*, worker_id: str = "worker") -> int:
        calls.append(worker_id)
        return 0

    _fake_module(monkeypatch, "app.jobs.worker", run_once=run_once)
    _set_env(monkeypatch, EMBEDDED_WORKER="true")
    with TestClient(create_app()):
        deadline = time.monotonic() + 3
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
    assert calls and set(calls) == {"embedded"}
    settled = len(calls)
    time.sleep(0.1)
    assert len(calls) == settled  # loop stopped with the app


def test_worker_not_started_by_default(settings_env: object, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _fake_module(monkeypatch, "app.jobs.worker", run_once=lambda **_: calls.append("x") or 0)
    with TestClient(create_app()):
        time.sleep(0.05)
    assert calls == []


def test_worker_errors_do_not_kill_the_loop(settings_env: object, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def run_once(*, worker_id: str = "worker") -> int:
        calls.append(1)
        raise RuntimeError("boom")

    _fake_module(monkeypatch, "app.jobs.worker", run_once=run_once)
    _set_env(monkeypatch, EMBEDDED_WORKER="true")
    with TestClient(create_app()) as client:
        deadline = time.monotonic() + 3
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
        assert client.get("/health").status_code == 200
    assert calls


def _wait_until(condition: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    return condition()


def test_embedded_worker_sleeps_between_heartbeats_and_wakes_on_enqueue(
    settings_env: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Manufact the worker heartbeats every WORKER_IDLE_SECONDS (900 by default, so Neon can scale to zero) and is
    woken in-process the moment a job is committed (spec section 20)."""
    calls: list[str] = []

    def run_once(*, worker_id: str = "worker") -> int:
        calls.append(worker_id)
        return 0

    _fake_module(monkeypatch, "app.jobs.worker", run_once=run_once)
    _set_env(monkeypatch, EMBEDDED_WORKER="true")
    assert get_settings().worker_idle == 900.0
    started = time.monotonic()
    with TestClient(create_app()):
        assert _wait_until(lambda: len(calls) == 1)
        time.sleep(0.3)
        assert len(calls) == 1  # asleep: it is not polling the database
        dispatcher.wakeup.set()  # what `dispatcher.enqueue` does after the commit
        assert _wait_until(lambda: len(calls) == 2)
        time.sleep(0.2)
        assert len(calls) == 2  # woken once, back to sleep
    assert time.monotonic() - started < 8  # shutdown woke the sleeping worker instead of waiting out 900 s


def test_embedded_worker_polls_at_worker_idle_seconds(settings_env: object, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def run_once(*, worker_id: str = "worker") -> int:
        calls.append(worker_id)
        return 0

    _fake_module(monkeypatch, "app.jobs.worker", run_once=run_once)
    _set_env(monkeypatch, EMBEDDED_WORKER="true", WORKER_IDLE_SECONDS="0.02")
    with TestClient(create_app()):
        assert _wait_until(lambda: len(calls) >= 4)


def test_embedded_worker_drains_a_busy_table_without_sleeping(
    settings_env: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While `run_once` keeps handling jobs the loop goes straight back for the next one."""
    remaining = [5]

    def run_once(*, worker_id: str = "worker") -> int:
        if remaining[0] == 0:
            return 0
        remaining[0] -= 1
        return 1

    _fake_module(monkeypatch, "app.jobs.worker", run_once=run_once)
    _set_env(monkeypatch, EMBEDDED_WORKER="true")  # idle 900 s: only an unslept loop can get through five jobs
    with TestClient(create_app()):
        assert _wait_until(lambda: remaining[0] == 0)
