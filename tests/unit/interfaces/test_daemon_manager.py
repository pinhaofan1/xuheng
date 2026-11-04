from interfaces.api.settings import BackendSettings
from threading import Event
from unittest.mock import Mock
import pytest
from interfaces.daemon_manager import (
    AutopilotDaemonManager,
    DaemonLifecycleSettings,
    DaemonStatus,
    is_expected_daemon_shutdown_exception,
)


class FakeEvent:
    def __init__(self):
        self.was_set = False

    def set(self):
        self.was_set = True


class FakeProcess:
    pid = 1234

    def __init__(self, *, alive=True):
        self.alive = alive
        self.join_calls = []
        self.terminated = False

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.join_calls.append(timeout)

    def terminate(self):
        self.terminated = True
        self.alive = False


def test_expected_daemon_shutdown_exception_detects_chained_interrupt():
    exc = RuntimeError("wrapper")
    exc.__cause__ = KeyboardInterrupt()

    assert is_expected_daemon_shutdown_exception(exc) is True


def test_cleanup_never_kills_a_venv_parent_or_other_python_server(monkeypatch):
    from interfaces.daemon_manager import cleanup_orphan_python_processes
    def forbid(*args, **kwargs):
        raise AssertionError("Global process enumeration/termination must not run")
    monkeypatch.setattr("interfaces.daemon_manager.subprocess.run", forbid)
    cleanup_orphan_python_processes()


def test_daemon_manager_status_reads_process_state():
    process = FakeProcess(alive=True)
    manager = AutopilotDaemonManager(
        log_level=20,
        log_file="logs/test.log",
        shared_state_provider=lambda: {},
        lifecycle_settings_provider=lambda: DaemonLifecycleSettings(
            graceful_join_timeout_seconds=0.25,
            terminate_join_timeout_seconds=0.5,
        ),
    )
    manager.process = process

    assert manager.status() == DaemonStatus(running=True, pid=1234)


def test_daemon_manager_start_respects_disable_auto_daemon():
    calls = []
    manager = AutopilotDaemonManager(
        log_level=20,
        log_file="logs/test.log",
        shared_state_provider=lambda: {},
        settings_provider=lambda: BackendSettings(disable_auto_daemon=True),
        process_factory=lambda **kwargs: calls.append(kwargs),
    )

    manager.start()

    assert calls == []
    assert manager.process is None


def test_daemon_manager_stop_signals_and_terminates_stuck_process(monkeypatch):
    process = FakeProcess(alive=True)
    event = FakeEvent()
    manager = AutopilotDaemonManager(
        log_level=20,
        log_file="logs/test.log",
        shared_state_provider=lambda: {},
        lifecycle_settings_provider=lambda: DaemonLifecycleSettings(
            graceful_join_timeout_seconds=0.25,
            terminate_join_timeout_seconds=0.5,
        ),
    )
    manager.process = process
    manager.stop_event = event
    monkeypatch.setattr("interfaces.daemon_manager.os.name", "posix")

    manager.stop()

    assert event.was_set is True
    assert process.terminated is True
    assert process.join_calls == [0.25, 0.5]
    assert manager.process is None
    assert manager.stop_event is None


def test_explicit_start_bypasses_offline_startup_and_reuses_worker(monkeypatch):
    manager = AutopilotDaemonManager(
        log_level=20, log_file="logs/test.log", shared_state_provider=lambda: {},
        settings_provider=lambda: BackendSettings(disable_auto_daemon=True),
        event_factory=Event,
    )
    monkeypatch.setattr("application.engine.services.streaming_bus.init_streaming_bus", lambda: None)
    monkeypatch.setattr("application.engine.services.shared_state_repository.init_shared_state_repository", lambda _: None)
    monkeypatch.setattr("application.engine.services.state_bootstrap.bootstrap_state", lambda: {})
    monkeypatch.setattr("application.engine.services.query_service.init_query_service", lambda _: None)
    monkeypatch.setattr("application.engine.services.persistence_queue.initialize_persistence_queue", lambda: None)
    monkeypatch.setattr("application.engine.services.persistence_queue.register_persistence_handlers", lambda: None)
    monkeypatch.setattr("application.engine.services.persistence_queue.get_persistence_queue",
                        lambda: SimpleQueue())
    process = FakeProcess()
    process.start = lambda: manager.ready_event.set()
    factory = Mock(return_value=process)
    manager._process_factory = factory
    manager.start()
    assert factory.call_count == 0
    manager.ensure_running(timeout=0.5)
    manager.ensure_running(timeout=0.5)
    assert factory.call_count == 1
    assert manager.status().running is True


class SimpleQueue:
    def is_consumer_running(self):
        return True


def test_worker_readiness_failure_is_reported_and_cleaned_up(monkeypatch):
    manager = AutopilotDaemonManager(log_level=20, log_file="logs/test.log", shared_state_provider=lambda: {})
    monkeypatch.setattr(manager, "start", lambda **kw: None)
    manager.process = FakeProcess(alive=False)
    manager.ready_event = Event()
    cleanup = Mock()
    monkeypatch.setattr(manager, "stop", cleanup)
    with pytest.raises(RuntimeError, match="初始化失败"):
        manager.ensure_running(timeout=0.1)
    cleanup.assert_called_once()
