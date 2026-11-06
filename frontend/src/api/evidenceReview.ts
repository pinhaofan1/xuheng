/** Xuheng evidence review extension; explicit constraints, no implicit AI calls. */
import { apiClient } from './config'

export interface CharacterRule {
  id: string
  name: string
  aliases: string[]
  allowed_magic: string[]
  forbidden_actions: string[]
  facts?: string[]
}
export interface RuleSet { revision: string; profiles: CharacterRule[] }
export type ReviewStatus = 'pending' | 'confirmed' | 'ignored'
export type RetrievalMode = 'bm25' | 'hybrid' | 'hybrid_rerank'
export interface EvidenceIssue {
  id: string
  kind: 'conflict' | 'uncertain'
  entity_id: string | null
  entity_name: string | null
  action: string
  start: number
  end: number
  quote: string
  source_id: string | null
  source_quote: string
  source_hash: string
  description: string
  status: ReviewStatus
  feedback_version: number
  source_kind?: 'setting' | 'history'
  source_chapter_id?: string | null
  source_chapter_number?: number | null
  source_start?: number | null
  source_end?: number | null
  origin?: 'rules' | 'ai'
}
export interface EvidenceRun {
  id: string
  chapter_id: string
  chapter_number: number
  content_snapshot: string
  content_hash: string
  created_at: string
  elapsed_ms: number
  issues: EvidenceIssue[]
  uncertain: EvidenceIssue[]
  stale: boolean
  stale_reasons: string[]
  offset_unit: 'unicode_codepoint'
  scope: string
  model_calls: number
  mode: 'rules' | 'ai'
  model_name?: string
  input_tokens?: number | null
  output_tokens?: number | null
  summary?: string
  warnings?: string[]
  sources?: HistorySource[]
}
export interface HistorySource { id: string; chapter_id: string; chapter_number: number; start: number; end: number; text: string; content_hash: string; score?: number }
export interface RetrievedHistory { sources: HistorySource[]; mode: string; indexed_chapters: number; indexed_chunks: number; warnings: string[]; scope: string; retrieval_usage?: Record<string, unknown> }
export interface RetrievalConfig { configured: boolean; base_url: string; embedding_model: string; reranker_model: string; dimensions: number; api_key_configured: boolean; credential_source: string }
export interface ReviewJob { id: string; novel_id: string; number: number; status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted'; stage: string; run_id: string | null; error: string; cancel_requested: boolean; created_at: string; updated_at: string; retry_of?: string | null }
export interface ModelStatus { configured: boolean; model: string; message: string }
export interface RunHistoryItem { id: string; mode: string; model_name: string; created_at: string; elapsed_ms: number; input_tokens: number | null; output_tokens: number | null }
export type FactStatus = 'pending' | 'accept' | 'coexist' | 'reject'
export type FactAction = Exclude<FactStatus, 'pending'> | 'undo'
export interface FactState {
  status: FactStatus; version: number; profile_id: string | null; replaces: string | null
  applies_from: number | null; applies_through: number | null
}
export interface FactCandidate extends FactState {
  id: string; subject_name: string; assertion: string; source_number: number; source_quote: string; source_valid: boolean
  source_start: number; source_end: number
}
export interface FactPage { facts: FactCandidate[]; has_more: boolean; rules_revision: string }
export interface FactComparison { profile_name: string; settings: string[]; related: FactCandidate[] }
export interface FactEvent { action: FactAction; before: FactState; after: FactState; created_at: string }
export interface FactDecision {
  request_id: string; expected_version: number; rules_revision: string; action: FactAction
  profile_id?: string | null; replaces?: string | null; applies_from?: number | null; applies_through?: number | null
}
const root = (novel: string) => `/evidence-reviews/${encodeURIComponent(novel)}`
export const evidenceReviewApi = {
  facts: (novel: string, status: FactStatus | null, offset: number) =>
    apiClient.get<FactPage>(`${root(novel)}/facts`, { params: { status, offset } }) as Promise<FactPage>,
  factComparison: (novel: string, fact: string, profileId: string) =>
    apiClient.get<FactComparison>(`${root(novel)}/facts/${fact}/comparison`, { params: { profile_id: profileId } }) as Promise<FactComparison>,
  factHistory: (novel: string, fact: string) =>
    apiClient.get<FactEvent[]>(`${root(novel)}/facts/${fact}/history`) as Promise<FactEvent[]>,
  decideFact: (novel: string, fact: string, body: FactDecision) =>
    apiClient.post<FactState>(`${root(novel)}/facts/${fact}/decisions`, body) as Promise<FactState>,
  rules: (novel: string) => apiClient.get<RuleSet>(`${root(novel)}/rules`) as Promise<RuleSet>,
  saveRules: (novel: string, rules: RuleSet) => apiClient.put<RuleSet>(`${root(novel)}/rules`, rules) as Promise<RuleSet>,
  importCharacters: (novel: string) => apiClient.get<{profiles:CharacterRule[];warnings:string[];source:string}>(`${root(novel)}/rules/import-preview`) as Promise<{profiles:CharacterRule[];warnings:string[];source:string}>,
  latest: (novel: string, chapter: number, mode = 'rules') => apiClient.get<EvidenceRun | null>(`${root(novel)}/chapters/${chapter}/latest?mode=${mode}`) as Promise<EvidenceRun | null>,
  getRun: (novel: string, run: string) => apiClient.get<EvidenceRun>(`${root(novel)}/runs/${run}`) as Promise<EvidenceRun>,
  modelStatus: () => apiClient.get<ModelStatus>('/evidence-reviews/model/status') as Promise<ModelStatus>,
  history: (novel: string, chapter: number) => apiClient.get<RunHistoryItem[]>(`${root(novel)}/chapters/${chapter}/runs`) as Promise<RunHistoryItem[]>,
  retrieve: (novel: string, chapter: number, query: string, semantic: boolean, retrievalMode: RetrievalMode = 'bm25') => apiClient.post<RetrievedHistory>(`${root(novel)}/chapters/${chapter}/context`, { query, semantic, retrieval_mode: retrievalMode, limit: 6 }, { timeout: 300000 }) as Promise<RetrievedHistory>,
  startAI: (novel: string, chapter: number, content: string, semantic: boolean, outputTokens: number, retrievalMode: RetrievalMode = 'bm25') => apiClient.post<ReviewJob>(`${root(novel)}/chapters/${chapter}/ai`, { expected_content: content, semantic, output_tokens: outputTokens, retrieval_mode: retrievalMode }) as Promise<ReviewJob>,
  retrievalConfig: () => apiClient.get<RetrievalConfig>('/evidence-reviews/retrieval/config') as Promise<RetrievalConfig>,
  saveRetrievalConfig: (config: Omit<RetrievalConfig, 'configured' | 'api_key_configured' | 'credential_source'> & { api_key?: string }) => apiClient.put<RetrievalConfig>('/evidence-reviews/retrieval/config', config) as Promise<RetrievalConfig>,
  testRetrieval: () => apiClient.post<{ok: boolean; dimension: number; usage: Record<string, unknown>}>('/evidence-reviews/retrieval/test', {}, { timeout: 120000 }) as Promise<{ok: boolean; dimension: number; usage: Record<string, unknown>}>,
  activeJob: (novel: string, chapter: number) => apiClient.get<ReviewJob | null>(`${root(novel)}/chapters/${chapter}/active-job`) as Promise<ReviewJob | null>,
  job: (novel: string, job: string) => apiClient.get<ReviewJob>(`${root(novel)}/jobs/${job}`) as Promise<ReviewJob>,
  cancelJob: (novel: string, job: string) => apiClient.post<ReviewJob>(`${root(novel)}/jobs/${job}/cancel`, {}) as Promise<ReviewJob>,
  run: (novel: string, chapter: number, content: string) => apiClient.post<EvidenceRun>(`${root(novel)}/chapters/${chapter}`, { expected_content: content }) as Promise<EvidenceRun>,
  feedback: (novel: string, run: string, issue: EvidenceIssue, status: ReviewStatus) =>
    apiClient.patch<EvidenceRun>(`${root(novel)}/runs/${run}/issues/${issue.id}`, {
      status, expected_version: issue.feedback_version,
    }) as Promise<EvidenceRun>,
}
