"""章节保存后的统一管线：叙事落库、向量检索、文风、图谱推断与后台抽取。

供 HTTP 保存、托管连写、自动驾驶审计复用，避免：
- 索引用正文截断 vs 叙事层用 LLM 总结 两套逻辑；
- 文风既入队 VOICE_ANALYSIS 又同步 score_chapter 重复计算。

关键路径只保留下一章/审计立即需要的产物：
1. 章间桥段资产；
2. 分章叙事同步 + 张力评分 + 向量/伏笔/三元组/因果边/人物状态/债务；
3. 文风评分（若调用方已在同一正文上评分，则复用结果）。

其余护栏快照、治理提交、演进/世界线快照、道具同步、汇流点检查等
作为串行辅助阶段后台执行，避免写密集尾部工作阻塞下一章。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, TYPE_CHECKING

from domain.ai.services.llm_service import LLMService
from application.ai.durable_calls import current_work, durable_stage
from application.engine.services.durable_aftermath import effect

if TYPE_CHECKING:
    from application.world.services.knowledge_service import KnowledgeService

logger = logging.getLogger(__name__)


async def _timed_aftermath_stage(
    stage: str,
    novel_id: str,
    chapter_number: int,
    work: Callable[[], Awaitable[Any]],
) -> Any:
    started = time.perf_counter()
    try:
        return await durable_stage(stage, work)
    finally:
        elapsed = time.perf_counter() - started
        logger.info(
            "章后阶段完成 stage=%s novel=%s ch=%s elapsed=%.2fs",
            stage,
            novel_id,
            chapter_number,
            elapsed,
        )


class _SerializedAuxiliaryQueue:
    """Run non-critical chapter aftermath side effects off the critical path.

    Jobs are chained per event loop instead of launched concurrently. This keeps
    SQLite write-heavy auxiliary work from blocking the next chapter while also
    avoiding a thundering herd of background writes.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tail: asyncio.Task[Any] | None = None

    def enqueue(
        self,
        stage: str,
        novel_id: str,
        chapter_number: int,
        work: Callable[[], Awaitable[Any]],
    ) -> None:
        loop = asyncio.get_running_loop()
        previous = self._tail if self._loop is loop and self._tail and not self._tail.done() else None
        self._loop = loop
        self._tail = loop.create_task(
            self._run_after(previous, stage, novel_id, chapter_number, work)
        )

    async def drain(self) -> None:
        tail = self._tail
        if tail and not tail.done():
            await tail

    async def _run_after(
        self,
        previous: asyncio.Task[Any] | None,
        stage: str,
        novel_id: str,
        chapter_number: int,
        work: Callable[[], Awaitable[Any]],
    ) -> None:
        if previous is not None:
            try:
                await previous
            except Exception:
                # Previous jobs log their own failure; keep the chain alive.
                pass
        try:
            await _timed_aftermath_stage(stage, novel_id, chapter_number, work)
        except Exception as e:
            logger.warning(
                "章后辅助阶段失败 stage=%s novel=%s ch=%s: %s",
                stage,
                novel_id,
                chapter_number,
                e,
            )


_AUXILIARY_QUEUE = _SerializedAuxiliaryQueue()


async def infer_kg_from_chapter(novel_id: str, chapter_number: int) -> None:
    """结构树章节节点 → 知识图谱增量推断（与 HTTP 原 _try_infer_kg_chapter 一致）。"""
    try:
        from application.paths import get_db_path
        from infrastructure.persistence.database.connection import get_database
        from infrastructure.persistence.database.sqlite_knowledge_repository import SqliteKnowledgeRepository
        from infrastructure.persistence.database.triple_repository import TripleRepository
        from infrastructure.persistence.database.chapter_element_repository import ChapterElementRepository
        from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
        from application.world.services.knowledge_graph_service import KnowledgeGraphService

        db_path = current_work.get().store.db.db_path if current_work.get() is not None else get_db_path()
        kr = SqliteKnowledgeRepository(get_database())
        story_node_id = kr.find_story_node_id_for_chapter_number(novel_id, chapter_number)
        if not story_node_id:
            logger.debug("KG 推断跳过：章节 %d 无故事节点 novel=%s", chapter_number, novel_id)
            return

        kg_service = KnowledgeGraphService(
            TripleRepository(),
            ChapterElementRepository(db_path),
            StoryNodeRepository(db_path),
        )
        triples = await kg_service.infer_from_chapter(story_node_id)
        logger.debug("KG 推断完成 novel=%s ch=%d 新三元组=%d", novel_id, chapter_number, len(triples))
    except Exception as e:
        logger.warning("KG 推断失败 novel=%s ch=%d: %s", novel_id, chapter_number, e)


class ChapterAftermathPipeline:
    """章节保存后分析与落库的统一入口。

    V8 Feed-forward 升级：集成因果边提取、人物状态突变评估、叙事债务更新。
    """

    def __init__(
        self,
        knowledge_service: "KnowledgeService",
        chapter_indexing_service: Any,
        llm_service: LLMService,
        voice_drift_service: Any = None,
        triple_repository: Any = None,
        foreshadowing_repository: Any = None,
        storyline_repository: Any = None,
        chapter_repository: Any = None,
        plot_arc_repository: Any = None,
        narrative_event_repository: Any = None,
        # V8 Feed-forward: 新增仓储
        causal_edge_repository: Any = None,
        character_state_repository: Any = None,
        debt_repository: Any = None,
        bible_repository: Any = None,
        unified_checkpoint_service: Any = None,
        prop_lifecycle_syncer: Any = None,
        evolution_snapshot_service: Any = None,
        character_narrative_kernel: Any = None,
    ) -> None:
        self._knowledge = knowledge_service
        self._indexing = chapter_indexing_service
        self._llm = llm_service
        self._voice = voice_drift_service
        self._triple_repository = triple_repository
        self._foreshadowing_repository = foreshadowing_repository
        self._storyline_repository = storyline_repository
        self._chapter_repository = chapter_repository
        self._plot_arc_repository = plot_arc_repository
        self._narrative_event_repository = narrative_event_repository
        # V8 Feed-forward: 因果图谱 / 人物状态机 / 叙事债务
        self._causal_edge_repository = causal_edge_repository
        self._character_state_repository = character_state_repository
        self._debt_repository = debt_repository
        self._bible_repository = bible_repository
        self._unified_checkpoint = unified_checkpoint_service
        self._prop_syncer = prop_lifecycle_syncer
        self._evolution_snapshot_service = evolution_snapshot_service
        self._character_kernel = character_narrative_kernel

    def task_store(self):
        from infrastructure.persistence.database.aftermath_task_store import AftermathTaskStore
        db = getattr(self._chapter_repository, "db", None)
        if not isinstance(getattr(db, "db_path", None), str):
            db = getattr(getattr(self._knowledge, "knowledge_repository", None), "db", None)
        return AftermathTaskStore(db) if isinstance(getattr(db, "db_path", None), str) else None

    async def prepare_after_save(self, novel_id, number, content, micro_beats=None, voice_result=None):
        store = self.task_store()
        if store is None or not content.strip():
            return None
        from infrastructure.persistence.database.aftermath_task_store import TaskStale
        for attempt in range(51):
            try:
                return store.enqueue(novel_id, number, content, micro_beats, voice_result)
            except TaskStale:
                if attempt == 50:
                    raise
                await asyncio.sleep(.1)

    async def run_after_chapter_saved(
        self, novel_id: str, chapter_number: int, content: str,
        chapter_micro_beats=None, voice_result=None,
    ) -> Dict[str, Any]:
        store = self.task_store()
        if store is None or not content.strip():
            return await self._run_after_chapter_saved_legacy(novel_id, chapter_number, content, chapter_micro_beats, voice_result)
        from application.engine.services.durable_aftermath import DurableAftermath
        task = await self.prepare_after_save(novel_id, chapter_number, content, chapter_micro_beats, voice_result)
        result = await DurableAftermath(store, store.get(novel_id, task["id"], private=True)).execute(self, wait=True)
        return result

    async def _run_after_chapter_saved_legacy(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        chapter_micro_beats: Optional[List[Dict[str, Any]]] = None,
        voice_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """保存正文后执行完整管线。返回文风结果供托管/审计门控使用。

        三元组与伏笔、故事线、张力、对话、因果边、人物状态、债务
        已在 narrative_sync 单次 LLM 中落库。
        """
        out: Dict[str, Any] = {
            "drift_alert": False,
            "similarity_score": None,
            "narrative_sync_ok": False,
            "vector_stored": False,
            "foreshadow_stored": False,
            "triples_extracted": False,
            "causal_edges_stored": False,
            "character_mutations_stored": False,
            "debt_updated": False,
            "bridge_extracted": False,
            "guardrail_passed": None,
            "guardrail_score": None,
            "evolution_snapshot_ok": False,
            "evolution_snapshot_id": None,
            "character_reconcile_ok": False,
            "character_reconcile": None,
        }

        if not content or not str(content).strip():
            logger.debug("aftermath 跳过：正文为空 novel=%s ch=%s", novel_id, chapter_number)
            return out

        # 0) 章间衔接锚点。放在统一章后管线里，确保 HTTP 保存、托管连写、
        # 自动驾驶最终都会产出同一种前章桥段资产。
        try:
            await _timed_aftermath_stage(
                "bridge_extract",
                novel_id,
                chapter_number,
                lambda: self._extract_chapter_bridge(novel_id, chapter_number, content),
            )
            out["bridge_extracted"] = True
        except Exception as e:
            logger.warning("章节桥段提取失败 novel=%s ch=%s: %s", novel_id, chapter_number, e)
            if current_work.get() is not None:
                return out

        # 1) 叙事 + 向量 + 故事线 + 张力 + 对话 + 因果边 + 人物状态 + 债务
        try:
            from application.world.services.chapter_narrative_sync import (
                sync_chapter_narrative_after_save,
            )

            async def _sync_narrative() -> Dict[str, Any]:
                return await sync_chapter_narrative_after_save(
                    novel_id,
                    chapter_number,
                    content,
                    self._knowledge,
                    self._indexing,
                    self._llm,
                    triple_repository=self._triple_repository,
                    foreshadowing_repo=self._foreshadowing_repository,
                    storyline_repository=self._storyline_repository,
                    chapter_repository=self._chapter_repository,
                    plot_arc_repository=self._plot_arc_repository,
                    narrative_event_repository=self._narrative_event_repository,
                    causal_edge_repository=self._causal_edge_repository,
                    character_state_repository=self._character_state_repository,
                    debt_repository=self._debt_repository,
                    bible_repository=self._bible_repository,
                    chapter_micro_beats=chapter_micro_beats,
                )

            sync_flags = await _timed_aftermath_stage(
                "narrative_sync",
                novel_id,
                chapter_number,
                _sync_narrative,
            )
            out["narrative_sync_ok"] = bool(sync_flags.get("narrative_sync_ok",True))
            if not out["narrative_sync_ok"]:
                # Do not spend more tokens on optional work after the critical
                # extraction failed. The saved manuscript is retained for resume.
                return out
            out["vector_stored"] = bool(sync_flags.get("vector_stored"))
            out["foreshadow_stored"] = bool(sync_flags.get("foreshadow_stored"))
            out["triples_extracted"] = bool(sync_flags.get("triples_extracted"))
            out["causal_edges_stored"] = bool(sync_flags.get("causal_edges_stored"))
            out["character_mutations_stored"] = bool(sync_flags.get("character_mutations_stored"))
            out["debt_updated"] = bool(sync_flags.get("debt_updated"))
            # 传递多维张力评分（0-100），供审计流程替代旧式 _score_tension
            out["tension_composite"] = sync_flags.get("tension_composite")
        except Exception as e:
            logger.warning(
                "叙事同步/向量失败 novel=%s ch=%s: %s", novel_id, chapter_number, e
            )
            if current_work.get() is not None:
                return out

        # 1b) 角色叙事内核对账：cast plan vs 正文，自动投影状态与风险。
        try:
            if self._character_kernel:
                reconcile = effect("character_reconcile", lambda: self._character_kernel.reconcile_after_chapter(
                    novel_id,
                    chapter_number,
                    content,
                    None,
                ))
                out["character_reconcile_ok"] = bool(reconcile.get("checked"))
                out["character_reconcile"] = reconcile
        except Exception as e:
            logger.warning(
                "角色叙事对账失败 novel=%s ch=%s: %s", novel_id, chapter_number, e
            )

        # 2) 文风（落库 chapter_style_scores）
        # 支持 LLM 模式（异步）和统计模式（同步）
        if voice_result is not None:
            self._apply_voice_result(out, voice_result)
            out["voice_reused"] = True
            logger.debug(
                "复用文风评分 novel=%s ch=%s mode=%s drift=%s",
                novel_id,
                chapter_number,
                out.get("voice_mode"),
                out["drift_alert"],
            )
        elif self._voice:
            try:
                async def _score_voice() -> Dict[str, Any]:
                    if getattr(self._voice, "use_llm_mode", False):
                        return await self._voice.score_chapter_async(
                            novel_id=novel_id,
                            chapter_number=chapter_number,
                            content=content,
                        )
                    return self._voice.score_chapter(
                        novel_id=novel_id,
                        chapter_number=chapter_number,
                        content=content,
                    )

                vr = await _timed_aftermath_stage(
                    "voice_drift",
                    novel_id,
                    chapter_number,
                    _score_voice,
                )
                self._apply_voice_result(out, vr)
                out["voice_reused"] = False
                logger.debug(
                    "文风评分完成 novel=%s ch=%s mode=%s drift=%s",
                    novel_id,
                    chapter_number,
                    out.get("voice_mode"),
                    out["drift_alert"],
                )
            except Exception as e:
                logger.warning("文风评分失败 novel=%s ch=%s: %s", novel_id, chapter_number, e)

        out["auxiliary_deferred"] = True
        if current_work.get() is None:
            self._schedule_auxiliary_stages(novel_id, chapter_number, content, dict(out))

        return out

    @staticmethod
    def _apply_voice_result(out: Dict[str, Any], result: Dict[str, Any]) -> None:
        out["drift_alert"] = bool(result.get("drift_alert", False))
        out["similarity_score"] = result.get("similarity_score")
        out["voice_mode"] = result.get("mode", "statistics")

    async def drain_auxiliary_stages(self) -> None:
        """Wait for queued auxiliary aftermath work.

        Intended for tests and controlled shutdown paths; normal callers should
        not await this on the writing critical path.
        """
        await _AUXILIARY_QUEUE.drain()

    def _schedule_auxiliary_stages(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        evidence: Dict[str, Any],
    ) -> None:
        async def _job() -> None:
            await self._run_auxiliary_stages(novel_id, chapter_number, content, evidence)

        _AUXILIARY_QUEUE.enqueue(
            "auxiliary_after_chapter",
            novel_id,
            chapter_number,
            _job,
        )

    async def _run_auxiliary_stages(self, novel_id, chapter_number, content, evidence):
        async def kg():
            await asyncio.to_thread(lambda: effect("kg_apply", lambda: asyncio.run(infer_kg_from_chapter(novel_id, chapter_number))))
            return {}

        async def guardrail():
            import uuid
            from datetime import datetime, timezone
            from application.engine.services.guardrail_execution import run_guardrail_advise_sync
            from engine.core.ports.ports import TraceRecord
            from engine.infrastructure.persistence.trace_store import SqliteTraceStore
            from infrastructure.persistence.database.chapter_guardrail_snapshot_repository import ChapterGuardrailSnapshotRepository
            from infrastructure.persistence.database.connection import get_database
            started = time.perf_counter()
            dto = await asyncio.to_thread(run_guardrail_advise_sync, novel_id, content, f"第{chapter_number}章（保存后自动）")
            trace = TraceRecord(
                trace_id=str(uuid.uuid4()), node_type="guardrail", operation="chapter_after_save",
                input_summary=f"{novel_id} ch{chapter_number} len={len(content)}"[:200],
                output_summary=f"passed={dto.get('passed')} score={dto.get('overall_score')}"[:200],
                score=float(dto.get("overall_score") or 0),
                violations=[str(v.get("description", ""))[:120] for v in (dto.get("violations") or [])[:20] if isinstance(v, dict)],
                duration_ms=int((time.perf_counter()-started)*1000), timestamp=datetime.now(timezone.utc).isoformat(), novel_id=novel_id)
            async def persist():
                db = get_database()
                ChapterGuardrailSnapshotRepository(db).upsert(novel_id, chapter_number, dto)
                await SqliteTraceStore(db).record(trace)
            await asyncio.to_thread(lambda: effect("guardrail_apply", lambda: asyncio.run(persist())))
            return {"guardrail_passed": bool(dto.get("passed")), "guardrail_score": dto.get("overall_score")}

        async def governance():
            from application.governance.service import NarrativeGovernanceService
            from infrastructure.persistence.database.connection import get_database
            from infrastructure.persistence.database.sqlite_governance_repository import SqliteGovernanceRepository
            from infrastructure.persistence.database.sqlite_storyline_repository import SqliteStorylineRepository
            from interfaces.api.dependencies import get_novel_repository
            def commit():
                db = get_database()
                service = NarrativeGovernanceService(SqliteGovernanceRepository(db), get_novel_repository(), SqliteStorylineRepository(db), db)
                report = service.commit_chapter(novel_id, chapter_number, content, dict(evidence))
                return {"governance_report": report.to_dict(), "governance_severity": report.severity, "governance_should_pause": report.should_pause_autopilot}
            return await asyncio.to_thread(lambda: effect("governance_apply", commit))

        async def evolution():
            if not self._evolution_snapshot_service:
                return {}
            def commit():
                snapshot = self._evolution_snapshot_service.build_after_chapter_saved(novel_id, chapter_number, content, "main", dict(evidence))
                return {"evolution_snapshot_ok": snapshot.status == "active", "evolution_snapshot_id": snapshot.snapshot_id}
            return await asyncio.to_thread(lambda: effect("evolution_apply", commit))

        async def worldline():
            if not self._unified_checkpoint:
                return {}
            id = await asyncio.to_thread(lambda: effect("worldline_apply", lambda: self._unified_checkpoint.create_checkpoint(
                novel_id, "CHAPTER", f"第{chapter_number}章自动快照", None, "main", None, {"chapter": chapter_number})))
            return {"worldline_checkpoint_id": id}

        async def props():
            if not self._prop_syncer:
                return {}
            return {"prop_sync": await self._prop_syncer.sync(novel_id, chapter_number, content)}

        async def confluence():
            from interfaces.api.dependencies import get_confluence_point_repository
            hits = [cp.id for cp in get_confluence_point_repository().get_by_novel_id(novel_id) if cp.target_chapter == chapter_number and not cp.resolved]
            return {"confluence_points": hits}

        for name, operation in [("kg", kg), ("guardrail", guardrail), ("governance", governance),
                                ("evolution", evolution), ("worldline", worldline), ("props", props), ("confluence", confluence)]:
            try:
                result = await durable_stage("aux_"+name, operation)
                evidence.update(result or {})
            except Exception:
                if current_work.get() is not None:
                    raise
                logger.warning("辅助阶段失败 stage=%s novel=%s ch=%s", name, novel_id, chapter_number, exc_info=True)

    async def _extract_chapter_bridge(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
    ) -> None:
        """统一章后桥段提取。

        这是保存后管线的衔接端口，而不是某条写作路径的私有后处理。
        ChapterBridgeService 的写入是 upsert，重复调用保持幂等。
        """
        from application.engine.services.chapter_bridge_service import ChapterBridgeService
        from application.paths import get_db_path

        svc = ChapterBridgeService(
            llm_service=self._llm,
            db_path=str(get_db_path()),
        )
        await svc.extract_bridge(novel_id, chapter_number, content)
