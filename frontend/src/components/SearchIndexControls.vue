<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { apiClient } from '@/api/config'

const props = defineProps<{ novel: string }>()
type Status = {
  job: { id: string; status: string; error: string } | null
  vector_job: {
    id: string; status: string; error: string; current_model: boolean
    pending_confirmation: boolean; indexed_chunks: number; total_chunks: number
  } | null
  indexed_chapters: number
  total_chapters: number
}
const status = ref<Status | null>(null)
const error = ref('')
const busy = ref(false)
const labels: Record<string, string> = { queued: '正在构建', completed: '已完成', cancelled: '已暂停', failed: '失败' }
let version = 0
let timer: ReturnType<typeof setTimeout> | undefined

function path() { return `/novels/${encodeURIComponent(props.novel)}/index` }

async function refresh(expected: number) {
  try {
    const response = await apiClient.get<Status>(path())
    if (expected !== version) return
    status.value = response
    error.value = ''
    if (response.job || response.vector_job) timer = setTimeout(() => void refresh(expected), 2000)
  } catch {
    if (expected === version) error.value = '无法读取索引状态，请刷新重试。'
  }
}

async function action(suffix: string, body: object = {}) {
  clearTimeout(timer)
  const expected = ++version
  busy.value = true
  try {
    await apiClient.post(path() + suffix, body)
    if (expected === version) await refresh(expected)
  } catch (cause) {
    const detail = (cause as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
    if (expected === version) error.value = typeof detail === 'string' ? detail : '索引任务状态已变化或操作失败，请刷新重试。'
  } finally {
    if (expected === version) busy.value = false
  }
}

watch(() => props.novel, () => {
  clearTimeout(timer)
  status.value = null
  busy.value = false
  void refresh(++version)
}, { immediate: true })
onBeforeUnmount(() => { ++version; clearTimeout(timer) })
</script>

<template>
  <section class="index-controls" aria-label="本地历史索引">
    <strong>本地历史索引</strong>
    <span v-if="status" aria-live="polite">{{ status.indexed_chapters }}/{{ status.total_chapters }} 章 · {{ status.job ? labels[status.job.status] : '尚未构建' }}</span>
    <span v-if="error || status?.job?.error" role="status">{{ error || status?.job?.error }}</span>
    <n-space v-if="status || error" :size="6">
      <n-button v-if="status && !status.job" size="small" :disabled="busy" @click="action('/jobs')">构建索引</n-button>
      <n-button v-else-if="status?.job?.status === 'queued'" size="small" :disabled="busy" @click="action(`/jobs/${status.job.id}/cancel`)">暂停</n-button>
      <n-button v-else-if="status?.job && ['cancelled', 'failed'].includes(status.job.status)" size="small" :disabled="busy" @click="action(`/jobs/${status.job.id}/resume`)">继续构建</n-button>
      <n-button v-if="status?.job?.status === 'completed'" size="small" :disabled="busy" @click="action('/jobs', { rebuild: true })">重建索引</n-button>
      <n-button v-if="error" size="small" :disabled="busy" @click="refresh(++version)">刷新</n-button>
    </n-space>
    <template v-if="status">
      <strong>历史向量索引</strong>
      <span>构建时会向已配置的嵌入服务发送正文片段，可能产生费用。后续保存章节时增量更新。</span>
      <span v-if="status.vector_job" aria-live="polite">
        {{ status.vector_job.indexed_chunks }}/{{ status.vector_job.total_chunks }} 段 ·
        {{ status.vector_job.current_model ? labels[status.vector_job.status] : '模型配置已变化' }}
        <template v-if="status.vector_job.pending_confirmation"> · 有一批请求待完成或核对</template>
      </span>
      <span v-if="status.vector_job?.error" role="status">{{ status.vector_job.error }}</span>
      <n-space :size="6">
        <n-button v-if="status.vector_job?.status === 'queued'" size="small" :disabled="busy"
          @click="action(`/vector-jobs/${status.vector_job.id}/cancel`)">暂停向量构建</n-button>
        <n-button v-else-if="!status.vector_job?.pending_confirmation && (status.vector_job?.status !== 'completed' || !status.vector_job?.current_model)"
          size="small" :disabled="busy" @click="action('/vector-jobs')">
          {{ status.vector_job?.current_model ? '继续向量构建' : '构建向量索引' }}
        </n-button>
      </n-space>
    </template>
  </section>
</template>

<style scoped>
.index-controls { display: grid; gap: 6px; padding: 10px 0; font-size: 13px; }
</style>
