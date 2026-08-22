from collections.abc import Callable
from typing import ClassVar

import httpx
import pytest
from fashionjobs import main as app_main
from fashionjobs.config import LABELS, LOG_PREFIX, FashionJobsConfig, load_config
from fashionjobs.monitor import FashionJobsMonitor
from fashionjobs.types import FashionItem
from monitor.config import Env, RunnerConfig
from monitor.discord import HttpClient, Poster
from monitor.state import LoadedState
from monitor.types import Payload

WEBHOOK = "https://discord.com/api/webhooks/1/test"


def test_defaults_are_respectful_and_health_server_is_off() -> None:
    cfg = load_config({"DISCORD_WEBHOOK_URL": WEBHOOK})
    assert cfg.runner.poll_interval_sec == 600
    assert cfg.runner.poll_jitter_pct == 20
    assert cfg.runner.heartbeat_at is None
    assert not hasattr(cfg.runner, "health_port")
    assert LOG_PREFIX == "fashionjobs-monitor"
    assert LABELS.tracked_noun == "listing identity(ies)"


def test_operational_runner_values_can_be_overridden() -> None:
    cfg = load_config(
        {
            "DISCORD_WEBHOOK_URL": WEBHOOK,
            "POLL_INTERVAL_SEC": "900",
            "HEARTBEAT_AT": "07:00",
        }
    )
    assert cfg.runner.poll_interval_sec == 900
    assert cfg.runner.heartbeat_at == (7, 0)


def test_product_filters_are_not_configuration_fields() -> None:
    cfg = load_config({"DISCORD_WEBHOOK_URL": WEBHOOK, "KEYWORDS": "styliste"})
    assert not hasattr(cfg, "keywords")
    assert not hasattr(cfg, "contract")
    assert not hasattr(cfg, "location")


class _RecordingSource:
    clients: ClassVar[list[httpx.AsyncClient]] = []
    initial_keys: ClassVar[list[set[str] | None]] = []

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        initial_keys: set[str] | None,
        log: Callable[[str], None],
    ) -> None:
        del log
        self.clients.append(client)
        self.initial_keys.append(initial_keys)

    async def fetch(self) -> list[FashionItem]:
        return []


@pytest.mark.asyncio
async def test_process_seeds_exact_state_and_reuses_one_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded_keys: set[str] = set()
    seeded_state = LoadedState(keys=seeded_keys, corrupt=False)
    state_paths: list[str] = []
    discord_clients: list[HttpClient] = []
    runner_configs: list[RunnerConfig] = []
    runner_states: list[LoadedState] = []
    logs: list[str] = []
    shutdown_logs: list[Callable[[str], None]] = []

    _RecordingSource.clients = []
    _RecordingSource.initial_keys = []

    def fake_load_config(_env: Env) -> FashionJobsConfig:
        return load_config(
            {
                "DISCORD_WEBHOOK_URL": WEBHOOK,
                "STATE_PATH": "/tmp/fashionjobs-task-7-state.json",
            }
        )

    def fake_load_state(path: str) -> LoadedState:
        state_paths.append(path)
        return seeded_state

    def fake_install_shutdown_handlers(log: Callable[[str], None]) -> None:
        shutdown_logs.append(log)

    async def fake_post(_url: str, _payload: Payload, client: HttpClient) -> None:
        discord_clients.append(client)

    async def fake_run_forever(
        monitor: FashionJobsMonitor,
        cfg: RunnerConfig,
        *,
        poster: Poster,
        now_unix: Callable[[], int],
        log: Callable[[str], None],
        preloaded_state: LoadedState,
    ) -> None:
        del monitor, now_unix, log
        runner_configs.append(cfg)
        runner_states.append(preloaded_state)
        await poster(WEBHOOK, Payload(embeds=()))

    monkeypatch.setattr(app_main, "load_config", fake_load_config)
    monkeypatch.setattr(app_main, "load_state", fake_load_state)
    monkeypatch.setattr(app_main, "install_shutdown_handlers", fake_install_shutdown_handlers)
    monkeypatch.setattr(app_main, "FashionJobsSource", _RecordingSource)
    monkeypatch.setattr(app_main, "post", fake_post)
    monkeypatch.setattr(app_main, "run_forever", fake_run_forever)
    monkeypatch.setattr(app_main, "log", logs.append)

    await app_main._main()

    assert state_paths == ["/tmp/fashionjobs-task-7-state.json"]
    assert _RecordingSource.initial_keys == [seeded_keys]
    assert _RecordingSource.initial_keys[0] is seeded_keys
    assert len(_RecordingSource.clients) == 1
    client = _RecordingSource.clients[0]
    assert client.timeout == httpx.Timeout(20)
    assert client.follow_redirects is False
    assert client.headers["Accept"] == "text/html"
    assert client.headers["User-Agent"] == "fashionjobs-monitor/2.0"
    assert discord_clients == [client]
    assert len(runner_configs) == 1
    assert runner_states == [seeded_state]
    assert runner_states[0] is seeded_state
    assert _RecordingSource.initial_keys[0] is runner_states[0].keys
    assert shutdown_logs == [logs.append]
    assert logs == ["app config — filter=Stage country=France keywords=none interval=600s"]


@pytest.mark.asyncio
async def test_process_marks_noncanonical_state_corrupt_before_source_and_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_state = LoadedState(keys={" 800 "}, corrupt=False)
    runner_states: list[LoadedState] = []

    _RecordingSource.clients = []
    _RecordingSource.initial_keys = []

    def fake_load_config(_env: Env) -> FashionJobsConfig:
        return load_config({"DISCORD_WEBHOOK_URL": WEBHOOK})

    async def fake_run_forever(
        monitor: FashionJobsMonitor,
        cfg: RunnerConfig,
        *,
        poster: Poster,
        now_unix: Callable[[], int],
        log: Callable[[str], None],
        preloaded_state: LoadedState,
    ) -> None:
        del monitor, cfg, poster, now_unix, log
        runner_states.append(preloaded_state)

    monkeypatch.setattr(app_main, "load_config", fake_load_config)
    monkeypatch.setattr(app_main, "load_state", lambda _path: invalid_state)
    monkeypatch.setattr(app_main, "install_shutdown_handlers", lambda _log: None)
    monkeypatch.setattr(app_main, "FashionJobsSource", _RecordingSource)
    monkeypatch.setattr(app_main, "run_forever", fake_run_forever)
    monkeypatch.setattr(app_main, "log", lambda _message: None)

    await app_main._main()

    assert _RecordingSource.initial_keys == [None]
    assert runner_states == [LoadedState(keys=None, corrupt=True)]
