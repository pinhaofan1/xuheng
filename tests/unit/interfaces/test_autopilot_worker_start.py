from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from interfaces.api.v1.engine import autopilot_routes as routes


@pytest.fixture
def cockpit(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    calls = []
    worker = SimpleNamespace(
        ensure_running=lambda: calls.append("worker_ready"),
        status=lambda: SimpleNamespace(running=False),
    )
    app.state.autopilot_daemon_provider = lambda: worker
    monkeypatch.setattr(routes, "_get_shared_state_for_novel", lambda _: {
        "_updated_at": 1, "current_stage": "macro_planning", "target_chapters": 3,
    })
    monkeypatch.setattr("interfaces.runtime_state.update_shared_novel_state",
                        lambda *a, **kw: calls.append(("state", kw)))

    def persist(*args, **kwargs):
        calls.append("persisted")
        return {"decision": SimpleNamespace(next_stage="macro_planning", reason="fresh"), "run_epoch": 1}

    monkeypatch.setattr(routes, "_persist_autopilot_running_sync", persist)
    monkeypatch.setattr("application.engine.services.novel_stop_signal.publish_start_signal",
                        lambda _: calls.append("signal"))
    return TestClient(app), worker, calls


def test_start_waits_for_worker_and_persistence_before_advertising_running(cockpit):
    client, _, calls = cockpit
    response = client.post("/autopilot/test/start", json={"target_chapters": 3})
    assert response.status_code == 200
    assert calls[0:2] == ["worker_ready", "persisted"]
    assert calls[2][0] == "state"
    assert calls[2][1]["autopilot_status"] == "running"
    assert calls[-1] == "signal"


def test_start_returns_503_without_running_state_when_worker_fails(cockpit):
    client, worker, calls = cockpit
    worker.ensure_running = Mock(side_effect=RuntimeError("child initialization failed"))
    response = client.post("/autopilot/test/start", json={})
    assert response.status_code == 503
    assert calls == []


def test_failed_persistence_does_not_publish_running(cockpit, monkeypatch):
    client, _, calls = cockpit
    monkeypatch.setattr(routes, "_persist_autopilot_running_sync", Mock(side_effect=RuntimeError("locked")))
    response = client.post("/autopilot/test/start", json={})
    assert response.status_code == 503
    assert calls == ["worker_ready"]


def test_dead_worker_overrides_shared_state_and_stale_heartbeat(cockpit, monkeypatch):
    client, _, _ = cockpit
    query = SimpleNamespace(get_novel_status_dict=lambda _: {
        "autopilot_status": "running", "daemon_alive": True, "_from_shared_memory": True,
    })
    monkeypatch.setattr("application.engine.services.query_service.get_query_service", lambda: query)
    response = client.get("/autopilot/test/status")
    assert response.status_code == 200
    assert response.json()["daemon_alive"] is False
    assert response.json()["daemon_process_running"] is False


def test_resume_does_not_publish_running_if_worker_cannot_start(cockpit, monkeypatch):
    client, worker, calls = cockpit
    monkeypatch.setattr(routes, "_get_shared_state_for_novel", lambda _: {
        "_updated_at": 1, "current_stage": "paused_for_review", "current_act": 0,
    })
    monkeypatch.setattr(routes, "_has_chapter_nodes_under_current_act", lambda *a: False)
    monkeypatch.setattr(routes, "_macro_structure_exists", lambda _: True)
    worker.ensure_running = Mock(side_effect=RuntimeError("child failed"))
    response = client.post("/autopilot/test/resume")
    assert response.status_code == 503
    assert calls == []


def test_resume_does_not_publish_running_if_persistence_fails(cockpit, monkeypatch):
    client, _, calls = cockpit
    monkeypatch.setattr(routes, "_get_shared_state_for_novel", lambda _: {
        "_updated_at": 1, "current_stage": "paused_for_review", "current_act": 0,
    })
    monkeypatch.setattr(routes, "_has_chapter_nodes_under_current_act", lambda *a: False)
    monkeypatch.setattr(routes, "_macro_structure_exists", lambda _: True)
    monkeypatch.setattr(routes, "get_novel_repository", lambda: SimpleNamespace(get_by_id=lambda _: SimpleNamespace(target_chapters=3)))
    monkeypatch.setattr(routes, "_persist_autopilot_resume_sync", Mock(side_effect=RuntimeError("locked")))
    response = client.post("/autopilot/test/resume")
    assert response.status_code == 503
    assert calls == ["worker_ready"]
