"""Regression cases for the upstream whole-outline subject attribution bug."""
from application.audit.services.conflict_detection_service import ConflictDetectionService


def test_two_characters_do_not_inherit_each_others_magic():
    result = ConflictDetectionService().detect(
        "林烬释放火球，沈澜施展水系法术。",
        {"lin": {"magic_type": "火系"}, "shen": {"magic_type": "水系"}},
        {"林烬": "lin", "沈澜": "shen"},
    )
    assert result == []


def test_only_the_actual_actor_gets_a_conflict():
    result = ConflictDetectionService().detect(
        "林烬释放火球，沈澜释放火球。",
        {"lin": {"magic_type": "火系"}, "shen": {"magic_type": "水系"}},
        {"林烬": "lin", "沈澜": "shen"},
    )
    assert [(x.entity_id, x.actual) for x in result] == [("shen", "火系")]
