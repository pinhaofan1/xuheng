"""DaemonHostMixin Phase 7/8/9 测试"""
import pytest

from application.engine.services.autopilot_daemon import AutopilotDaemon
from engine.runtime.daemon_host import DaemonHostMixin
from engine.runtime.runner import StoryPipelineRunner


def test_autopilot_daemon_inherits_daemon_host_mixin():
    assert issubclass(AutopilotDaemon, DaemonHostMixin)


def test_story_pipeline_runner_inherits_daemon_host_mixin():
    assert issubclass(StoryPipelineRunner, DaemonHostMixin)


def test_daemon_host_mixin_provides_infrastructure_methods():
    assert hasattr(DaemonHostMixin, "_update_shared_state")
    assert hasattr(DaemonHostMixin, "_call_with_timeout")
    assert hasattr(DaemonHostMixin, "_find_next_unwritten_chapter_async")
    assert hasattr(DaemonHostMixin, "_flush_novel")


def test_autopilot_daemon_keeps_entrypoint_methods():
    assert hasattr(AutopilotDaemon, "run_forever")
    assert hasattr(AutopilotDaemon, "_process_novel")
    assert hasattr(AutopilotDaemon, "_handle_writing")


def test_autopilot_daemon_emits_deprecation_warning():
    with pytest.warns(DeprecationWarning, match="AutopilotDaemon"):
        AutopilotDaemon(
            novel_repository=object(),
            llm_service=object(),
            context_builder=None,
            background_task_service=None,
            planning_service=None,
            story_node_repo=None,
            chapter_repository=None,
        )


def test_story_pipeline_runner_is_self_hosted():
    runner = StoryPipelineRunner(
        novel_repository=object(),
        llm_service=object(),
        context_builder=None,
        background_task_service=None,
        planning_service=None,
        story_node_repo=None,
        chapter_repository=None,
        use_story_pipeline_for_writing=True,
    )
    assert runner.host is runner
    assert runner.use_story_pipeline_for_writing is True


@pytest.mark.parametrize("local,stored,expected", [
    ("stopped", "running", "stopped"), ("error", "running", "error"),
    ("running", "stopped", "stopped"), ("running", "running", "running"),
])
def test_database_running_does_not_undo_a_local_stop(local, stored, expected):
    from types import SimpleNamespace
    from domain.novel.entities.novel import AutopilotStatus

    host = SimpleNamespace(_read_autopilot_status_ephemeral=lambda _: AutopilotStatus(stored))
    novel = SimpleNamespace(novel_id="bounded", autopilot_status=AutopilotStatus(local))
    DaemonHostMixin._merge_autopilot_status_from_db(host, novel)
    assert novel.autopilot_status == AutopilotStatus(expected)


def test_chapter_aggregates_count_all_manuscripts_and_exclude_empty_plans(monkeypatch):
    import sqlite3
    from types import SimpleNamespace

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE chapters (novel_id TEXT, status TEXT, content TEXT)")
    db.executemany("INSERT INTO chapters VALUES ('bounded', ?, ?)", [
        ("completed", "甲乙"), ("completed", "丙丁戊"), ("draft", "己"), ("draft", ""),
    ])
    adapter = SimpleNamespace(fetch_all=lambda sql, args: [dict(row) for row in db.execute(sql, args)])
    monkeypatch.setattr("infrastructure.persistence.database.connection.get_database", lambda *a: adapter)
    assert DaemonHostMixin._read_chapter_stats_ephemeral(SimpleNamespace(), "bounded") == (2, 3, 6)
    db.close()

