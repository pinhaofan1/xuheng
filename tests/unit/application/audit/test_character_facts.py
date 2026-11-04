import json
import pytest
from application.audit.evidence_models import CharacterRule, RuleSet, text_hash
from application.audit.services.character_import import preview_character_rules
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.evidence_review_store import EvidenceReviewStore


def test_empty_facts_preserve_legacy_rule_hash():
    profile = CharacterRule(id='person', name='沈澜')
    legacy = profile.model_dump(exclude={'facts'})
    assert RuleSet(profiles=[profile]).content_hash() == text_hash(json.dumps([legacy],ensure_ascii=False,sort_keys=True))


def test_facts_persist_and_import_is_a_preview_with_no_inferred_bans(tmp_path):
    db = DatabaseConnection(str(tmp_path / 'facts.db'))
    try:
        db.execute("INSERT INTO novels(id,title,slug) VALUES ('book','book','book')")
        db.execute("INSERT INTO unified_characters(id,novel_id,name,description) VALUES ('p','book','沈澜','左臂受伤，平时用剑。')")
        db.commit()
        store=EvidenceReviewStore(db)
        preview=preview_character_rules(store,'book')
        assert not store.get_rules('book').revision
        assert preview['profiles'][0]['facts']==['人物简介：左臂受伤，平时用剑。']
        assert preview['profiles'][0]['forbidden_actions']==[]
        saved=store.save_rules('book',RuleSet(profiles=[CharacterRule(**preview['profiles'][0])]))
        assert store.get_rules('book').profiles[0].facts==saved.profiles[0].facts
        before=saved.content_hash()
        saved.profiles[0].facts=['左臂已经康复。']
        after=store.save_rules('book',saved)
        assert after.content_hash()!=before
        assert '已经康复' in after.profiles[0].source_text()
    finally:
        db.close_all()


def test_facts_have_a_total_prompt_budget():
    with pytest.raises(ValueError, match='20000'):
        RuleSet(profiles=[CharacterRule(id=f'p{i}',name=f'人物{i}',facts=[str(j)+'字'*290 for j in range(24)]) for i in range(4)])
