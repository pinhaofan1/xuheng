import { apiClient } from './config'
import type { ReviewJob } from './evidenceReview'

export interface ModelPrice { kind: 'llm' | 'embedding' | 'rerank'; model: string; input_per_million: number; output_per_million: number }
export interface UsagePolicy { max_calls: number | null; max_tokens: number | null; max_cost: number | null; prices: ModelPrice[] }
export interface ModelCall { id: string; kind: string; model: string; status: string; created_at: string; updated_at: string; operation: string; input_tokens: number | null; output_tokens: number | null; reserved_tokens: number; cost: number | null; reserved_cost: number | null }
export interface Operations {
  chapter_progress?: {number:number;content_hash:string;stage:string;status:string;error:string;updated_at:string;stale:boolean;draft_available?:boolean;uncommitted?:boolean}[]
  budget_block: {message: string; created_at: string} | null
  policy: UsagePolicy
  totals: { calls: number; input_tokens: number; output_tokens: number; charged_tokens: number; estimated_cost: number; uncertain_calls: number; unknown_cost_calls: number }
  calls: ModelCall[]
  jobs: ReviewJob[]
}
const root = (novel: string) => `/evidence-reviews/${encodeURIComponent(novel)}`
export const operationsApi = {
  draft: (novel:string,number:number,hash:string) => apiClient.get<Blob>(`${root(novel)}/chapters/${number}/drafts/${hash}/export`,{responseType:'blob'}) as Promise<Blob>,
  get: (novel: string) => apiClient.get<Operations>(`${root(novel)}/operations`) as Promise<Operations>,
  savePolicy: (novel: string, policy: UsagePolicy) => apiClient.put<UsagePolicy>(`${root(novel)}/operations/policy`, policy) as Promise<UsagePolicy>,
  retry: (novel: string, job: string) => apiClient.post<ReviewJob>(`${root(novel)}/jobs/${job}/retry`, {}) as Promise<ReviewJob>,
}
