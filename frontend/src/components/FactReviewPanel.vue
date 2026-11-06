<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { evidenceReviewApi, type FactAction, type FactCandidate, type FactComparison, type FactDecision, type FactEvent, type FactPage, type FactStatus, type RuleSet } from '@/api/evidenceReview'
import { formatApiError, getHttpStatus } from '@/utils/apiError'

const props = defineProps<{ novel: string; disabled: boolean }>()
const emit = defineEmits<{ changed: [] }>()
const labels: Record<FactStatus, string> = { pending: '待确认', accept: '已接受', coexist: '保留并存', reject: '已拒绝' }
const filters = [{ value: 'all', label: '全部事实' }, ...Object.entries(labels).map(([value, label]) => ({ value, label }))]
const filter = ref<FactStatus | 'all'>('all')
const offset = ref(0)
const page = ref<FactPage | null>(null)
const rules = ref<RuleSet | null>(null)
const selectedId = ref('')
const selected = computed(() => page.value?.facts.find(fact => fact.id === selectedId.value))
const profile = ref<string | null>(null)
const replaces = ref<string | null>(null)
const from = ref<number | null>(null)
const through = ref<number | null>(null)
const comparison = ref<FactComparison | null>(null)
const history = ref<FactEvent[]>([])
const problem = ref('')
const detailError = ref('')
const loading = ref(false)
const saving = ref(false)
let loadSequence = 0
let lastAttempt: { signature: string; requestId: string } | null = null

async function load() {
  const sequence = ++loadSequence
  loading.value = true
  problem.value = ''
  try {
    const [result, settings] = await Promise.all([
      evidenceReviewApi.facts(props.novel, filter.value === 'all' ? null : filter.value, offset.value),
      evidenceReviewApi.rules(props.novel),
    ])
    if (sequence !== loadSequence) return
    page.value = result
    rules.value = settings
  } catch (error) {
    if (sequence === loadSequence) problem.value = formatApiError(error, '无法读取人物事实')
  } finally {
    if (sequence === loadSequence) loading.value = false
  }
}

function select(fact: FactCandidate) {
  if (fact.id === selectedId.value) return
  selectedId.value = fact.id
  const matches = rules.value?.profiles.filter(item => item.name === fact.subject_name || item.aliases.includes(fact.subject_name))
  profile.value = fact.profile_id || (matches?.length === 1 ? matches[0]!.id : null)
  from.value = fact.applies_from ?? fact.source_number + 1
  through.value = fact.applies_through
  replaces.value = fact.replaces
  lastAttempt = null
}

watch(() => props.novel, () => {
  page.value = null
  selectedId.value = ''
  offset.value = 0
  void load()
}, { immediate: true })

watch([filter, offset], () => { void load() })
watch([() => props.novel, selectedId, profile, () => page.value?.rules_revision], async ([novel, fact, person], _, cleanup) => {
  let current = true
  cleanup(() => { current = false })
  detailError.value = ''
  comparison.value = null
  history.value = []
  if (!fact) return
  try {
    const [records, related] = await Promise.all([
      evidenceReviewApi.factHistory(novel, fact),
      person ? evidenceReviewApi.factComparison(novel, fact, person) : Promise.resolve(null),
    ])
    if (current) { history.value = records; comparison.value = related }
  } catch (error) {
    if (current) detailError.value = formatApiError(error, '无法读取事实对照')
  }
})

async function decide(action: FactAction) {
  const fact = selected.value
  const novel = props.novel
  if (!fact || !page.value || saving.value || props.disabled) return
  const applies = action === 'accept' || action === 'coexist'
  const fields: Omit<FactDecision, 'request_id'> = {
    action, expected_version: fact.version, rules_revision: page.value.rules_revision,
    ...(applies ? { profile_id: profile.value, applies_from: from.value, applies_through: through.value } : {}),
    ...(action === 'accept' && replaces.value ? { replaces: replaces.value } : {}),
  }
  const signature = JSON.stringify([novel, fact.id, fields])
  if (lastAttempt?.signature !== signature) lastAttempt = { signature, requestId: crypto.randomUUID() }
  saving.value = true
  problem.value = ''
  try {
    await evidenceReviewApi.decideFact(novel, fact.id, { ...fields, request_id: lastAttempt.requestId })
    if (props.novel !== novel) return
    lastAttempt = null
    await load()
    emit('changed')
    const records = await evidenceReviewApi.factHistory(novel, fact.id)
    if (props.novel === novel && selectedId.value === fact.id) history.value = records
  } catch (error) {
    if (props.novel !== novel) return
    const explanation = formatApiError(error, '保存处理失败，请重试')
    if (getHttpStatus(error) === 409) await load()
    problem.value = explanation
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <section class="fact-panel" aria-label="人物事实候选与处理">
    <p>章后提取的事实由作者核对后生效。处理时可对照保存的人物设定和其他已确认事实。</p>
    <n-alert v-if="problem" type="error">{{ problem }}</n-alert>
    <n-space align="center">
      <n-select :value="filter" :options="filters" style="min-width: 132px" aria-label="筛选人物事实"
        @update:value="(value: FactStatus | 'all') => { filter = value; offset = 0 }" />
      <n-button secondary :loading="loading" :disabled="saving" @click="load">刷新事实</n-button>
    </n-space>
    <n-empty v-if="page && !page.facts.length && !loading" description="暂无符合条件的事实" />
    <button v-for="fact in page?.facts" :key="fact.id" type="button" class="fact-row"
      :aria-pressed="selectedId === fact.id" @click="select(fact)">
      <strong>{{ fact.subject_name }} · {{ fact.assertion }}</strong>
      <small>第 {{ fact.source_number }} 章 · {{ labels[fact.status] }}{{ fact.source_valid ? '' : ' · 依据已失效' }}</small>
    </button>
    <n-space v-if="offset > 0 || page?.has_more">
      <n-button size="small" :disabled="offset === 0 || loading" @click="offset -= 30">上一页</n-button>
      <n-button size="small" :disabled="!page?.has_more || loading" @click="offset += 30">下一页</n-button>
    </n-space>
    <article v-if="selected" class="fact-detail" aria-label="事实处理详情">
      <h3>{{ selected.subject_name }} · {{ selected.assertion }}</h3>
      <blockquote>{{ selected.source_quote }}</blockquote>
      <router-link v-if="selected.source_valid" :to="`/book/${novel}/chapter/${selected.source_number}`" target="_blank">查看来源章节 ↗</router-link>
      <n-alert v-else type="warning">来源正文或章节位置已变化，已停止将这条事实作为审校依据。</n-alert>
      <n-alert v-if="detailError" type="warning">{{ detailError }}</n-alert>
      <template v-if="selected.status === 'pending'">
        <n-select :value="profile" :options="rules?.profiles.map(person => ({ label: person.name, value: person.id })) || []"
          aria-label="对应审校人物" placeholder="选择已保存的审校人物" clearable
          @update:value="(value: string | null) => { profile = value; replaces = null }" />
        <n-space>
          <label>从第几章起<n-input-number v-model:value="from" :min="selected.source_number + 1" :max="100000" :precision="0" /></label>
          <label>至第几章<n-input-number v-model:value="through" placeholder="留空则长期适用" :min="from ?? selected.source_number + 1" :max="100000" :precision="0" /></label>
        </n-space>
      </template>
      <p v-else-if="selected.applies_from">适用第 {{ selected.applies_from }} 章起{{ selected.applies_through ? `，至第 ${selected.applies_through} 章` : '' }}</p>
      <template v-if="comparison">
        <h4>{{ comparison.profile_name }}的已保存设定</h4>
        <ul v-if="comparison.settings.length"><li v-for="setting in comparison.settings" :key="setting">{{ setting }}</li></ul>
        <p v-else>暂无文字设定可替换。</p>
        <n-select v-if="selected.status === 'pending' && comparison.settings.length" v-model:value="replaces" clearable
          aria-label="接受时替换的旧设定" placeholder="接受时可选一条旧设定替换"
          :options="comparison.settings.map(setting => ({ label: setting, value: setting }))" />
        <h4 v-if="comparison.related.length">其他已确认事实</h4>
        <ul v-if="comparison.related.length">
          <li v-for="related in comparison.related" :key="related.id">{{ related.assertion }}（来源第 {{ related.source_number }} 章，适用第 {{ related.applies_from }} 章起{{ related.applies_through ? `至第 ${related.applies_through} 章` : '' }}）</li>
        </ul>
      </template>
      <n-space v-if="selected.status === 'pending'">
        <n-button size="small" type="primary" :disabled="disabled || saving || !profile || !selected.source_valid" @click="decide('accept')">接受候选</n-button>
        <n-button size="small" :disabled="disabled || saving || !profile || !selected.source_valid" @click="decide('coexist')">保留并存</n-button>
        <n-button size="small" :disabled="disabled || saving" @click="decide('reject')">拒绝候选</n-button>
      </n-space>
      <n-button v-else size="small" :disabled="disabled || saving" @click="decide('undo')">撤销事实处理</n-button>
      <details v-if="history.length">
        <summary>处理记录（{{ history.length }}）</summary>
        <ul><li v-for="item in history" :key="item.after.version">
          {{ item.created_at }} · {{ labels[item.before.status] }} → {{ labels[item.after.status] }}
          <span v-if="item.after.replaces"> · 替换「{{ item.after.replaces }}」</span>
        </li></ul>
      </details>
    </article>
  </section>
</template>

<style scoped>
.fact-panel,.fact-detail{display:grid;gap:10px;min-width:0;overflow-wrap:anywhere}
.fact-panel p,.fact-detail h3,.fact-detail h4{margin:0}
.fact-row,.fact-detail{border:1px solid var(--app-border);border-radius:8px;padding:12px;background:var(--app-surface)}
.fact-row{text-align:left;color:inherit;display:grid;gap:4px;cursor:pointer}
.fact-row[aria-pressed="true"]{border-color:var(--color-brand)}
.fact-row small{color:var(--app-text-secondary)}
.fact-detail blockquote{margin:0;padding:8px 12px;border-left:2px solid var(--color-brand);white-space:pre-wrap}
.fact-detail ul{margin:0;padding-left:20px}
</style>
