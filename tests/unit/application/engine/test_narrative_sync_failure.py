from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock

import pytest

from application.world.services.chapter_narrative_sync import sync_chapter_narrative_after_save
from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline


@pytest.mark.asyncio
@pytest.mark.parametrize("response",[None,{}, {"summary":""},{"summary":123}])
async def test_bundle_failure_preserves_old_knowledge_and_does_not_make_more_model_calls(monkeypatch,response):
    knowledge=MagicMock()
    knowledge.get_knowledge.return_value=SimpleNamespace(chapters=[])
    extraction=AsyncMock(side_effect=OSError("fixture disconnected")) if response is None else AsyncMock(return_value=response)
    tension=AsyncMock(side_effect=AssertionError("No further paid calls after failed extraction"))
    monkeypatch.setattr("application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",extraction)
    monkeypatch.setattr("application.analyst.services.tension_scoring_service.TensionScoringService.score_chapter",tension)
    result=await sync_chapter_narrative_after_save("book",1,"正文内容",knowledge,None,object())
    assert not result["narrative_sync_ok"]
    knowledge.upsert_chapter_summary.assert_not_called()
    tension.assert_not_awaited()


@pytest.mark.asyncio
async def test_aftermath_critical_failure_does_not_schedule_optional_work(monkeypatch):
    voice=SimpleNamespace(use_llm_mode=True,score_chapter_async=AsyncMock())
    monkeypatch.setattr(ChapterAftermathPipeline,"_extract_chapter_bridge",AsyncMock())
    monkeypatch.setattr("application.world.services.chapter_narrative_sync.sync_chapter_narrative_after_save",AsyncMock(return_value={"narrative_sync_ok":False}))
    auxiliary=MagicMock()
    monkeypatch.setattr(ChapterAftermathPipeline,"_schedule_auxiliary_stages",auxiliary)
    pipeline=ChapterAftermathPipeline(knowledge_service=None,chapter_indexing_service=None,llm_service=object(),voice_drift_service=voice)
    result=await pipeline.run_after_chapter_saved("book",1,"正文内容")
    assert not result["narrative_sync_ok"]
    voice.score_chapter_async.assert_not_awaited()
    auxiliary.assert_not_called()
