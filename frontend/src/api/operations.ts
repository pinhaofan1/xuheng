import { apiClient } from './config'
import type { ReviewJob } from './evidenceReview'

export interface ModelPrice { kind: 'llm' | 'embedding' | 'rerank'; model: string; input_per_million: number; output_per_million: number }
export interface UsagePolicy { max_calls: number | null; max_tokens: number | null; max_cost: number | null; prices: ModelPrice[]; timezone: string; daily_max_calls: number | null; daily_max_tokens: number | null; daily_max_cost: number | null }
export interface ModelCall { id: string; kind: string; model: string; status: string; created_at: string; updated_at: string; operation: string; input_tokens: number | null; output_tokens: number | null; reserved_tokens: number; cost: number | null; reserved_cost: number | null; original_input_tokens: number | null; original_output_tokens: number | null; original_cost: number | null; revision: number; reconciled_at: string | null }
export interface UsageTotals { calls: number; input_tokens: number; output_tokens: number; charged_tokens: number; estimated_cost: number; uncertain_calls: number; unknown_cost_calls: number; reconciled_calls: number }
export interface ReconciliationRequest { input_tokens: number; output_tokens: number; billed_cost: number | null; expected_revision: number; request_key: string; note: string; reference: string; request_finished: true }
export interface Reconciliation { id: string; revision: number; note: string; reference: string; created_at: string; before: {input_tokens: number | null; output_tokens: number | null; cost: number | null; reserved_tokens: number; reserved_cost: number | null}; after: {input_tokens: number; output_tokens: number; cost: number | null; cost_source: string}; difference: {charged_tokens: number; cost: number | null} }
export interface AftermathTask { id: string; number: number; content_hash: string; status: string; phase: string; error: string; created_at: string; updated_at: string; stale: boolean; steps: string[]; unknown_requests: number; requests: {step: string; status: string; attempt: number; call_ids: string[]; created_at: string}[]; confirmations: {confirmed_unknown: number; note: string; created_at: string}[] }
export interface Operations {
  chapter_progress?: {number:number;content_hash:string;stage:string;status:string;error:string;updated_at:string;stale:boolean;draft_available?:boolean;uncommitted?:boolean}[]
  budget_block: {message: string; created_at: string} | null
  policy: UsagePolicy
  totals: UsageTotals
  daily: {date: string; timezone: string; start: string; end: string; totals: UsageTotals; models: (UsageTotals & {kind: string; model: string})[]}
  calls_page: {offset: number; total: number; limit: number}
  calls: ModelCall[]
  jobs: ReviewJob[]
  aftermath_tasks?: AftermathTask[]
}
const root = (novel: string) => `/evidence-reviews/${encodeURIComponent(novel)}`
export const operationsApi = {
  draft: (novel:string,number:number,hash:string) => apiClient.get<Blob>(`${root(novel)}/chapters/${number}/drafts/${hash}/export`,{responseType:'blob'}) as Promise<Blob>,
  get: (novel: string, params?: {day?: string; offset?: number; uncertain_only?: boolean}) => apiClient.get<Operations>(`${root(novel)}/operations`, {params}) as Promise<Operations>,
  reconcile: (novel: string, call: string, body: ReconciliationRequest) => apiClient.post<Reconciliation>(`${root(novel)}/operations/calls/${encodeURIComponent(call)}/reconciliations`, body) as Promise<Reconciliation>,
  reconciliations: (novel: string, call: string) => apiClient.get<Reconciliation[]>(`${root(novel)}/operations/calls/${encodeURIComponent(call)}/reconciliations`) as Promise<Reconciliation[]>,
  savePolicy: (novel: string, policy: UsagePolicy) => apiClient.put<UsagePolicy>(`${root(novel)}/operations/policy`, policy) as Promise<UsagePolicy>,
  retry: (novel: string, job: string) => apiClient.post<ReviewJob>(`${root(novel)}/jobs/${job}/retry`, {}) as Promise<ReviewJob>,
  resumeAftermath: (novel: string, task: string, body: {confirm_unknown: boolean; retry_model_calls: boolean; note: string}) => apiClient.post<AftermathTask>(`${root(novel)}/operations/aftermath/${encodeURIComponent(task)}/resume`, body) as Promise<AftermathTask>,
}
