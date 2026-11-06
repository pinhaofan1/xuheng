<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { apiClient } from '@/api/config'

const props = defineProps<{ novel: string }>()
type Status = {
  job: { id: string; status: string; error: string } | null
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
    if (response.job) timer = setTimeout(() => void refresh(expected), 2000)
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
  } catch {
    if (expected === version) error.value = '索引任务状态已变化或操作失败，请刷新重试。'
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
  </section>
</template>

<style scoped>
.index-controls { display: grid; gap: 6px; padding: 10px 0; font-size: 13px; }
</style>
