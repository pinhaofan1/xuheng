<!-- Xuheng extension: a review desk pairing exact manuscript spans with constraints. -->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { useDialog, useMessage } from 'naive-ui'
import { chapterApi } from '@/api/chapter'
import { evidenceReviewApi, type CharacterRule, type EvidenceIssue, type EvidenceRun, type ReviewStatus, type RuleSet, type RetrievedHistory, type ReviewJob, type RunHistoryItem, type ModelStatus, type RetrievalMode } from '@/api/evidenceReview'
import { formatApiError } from '@/utils/apiError'
import WorkspaceNav from '@/components/layout/WorkspaceNav.vue'
import AppIcon from '@/components/layout/AppIcon.vue'
import RetrievalSettings from '@/components/global/RetrievalSettings.vue'
import SearchIndexControls from '@/components/SearchIndexControls.vue'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const novel = computed(() => String(route.params.slug))
const number = computed(() => Number(route.params.id))
const routeKey = computed(() => `${novel.value}/${number.value}`)
const loading = ref(true)
const loadError = ref('')
const busy = ref(false)
const rules = ref<RuleSet>({ revision: '', profiles: [] })
const savedRules = ref('')
const savedContent = ref('')
const title = ref('')
const run = ref<EvidenceRun | null>(null)
const selectedId = ref('')
const filter = ref('all')
const selectedMark = ref<HTMLElement | null>(null)
const notice = ref('')
let loadSequence = 0
let pollTimer: ReturnType<typeof setTimeout> | undefined
const job = ref<ReviewJob | null>(null)
const model = ref<ModelStatus | null>(null)
const retrieval = ref<RetrievedHistory | null>(null)
const retrievalQuery = ref('')
const retrievalMode = ref<RetrievalMode>('bm25')
const retrievalModes = [{label:'关键词检索 · 本地',value:'bm25'},{label:'关键词＋语义召回',value:'hybrid'},{label:'混合召回＋模型重排',value:'hybrid_rerank'}]
const retrievalLabels: Record<string,string> = {'bm25':'关键词检索','bm25+vector':'关键词＋语义召回','bm25+vector+reranker':'混合召回＋模型重排','bm25+semantic-rerank':'旧版语义排序'}
const outputTokens = ref(4096)
const retrieving = ref(false)
const runHistory = ref<RunHistoryItem[]>([])
const panel = ref('results')
const settingsOpen = ref(false)
const panels = [{ value: 'results', label: '审校结果' }, { value: 'history', label: '历史检索' }, { value: 'runs', label: '运行历史' }]
const mode = computed(() => route.query.mode === 'ai' ? 'ai' : 'rules')
const jobActive = computed(() => job.value?.status === 'queued' || job.value?.status === 'running')

const serialize = (value: RuleSet) => JSON.stringify(value)
const rulesDirty = computed(() => serialize(rules.value) !== savedRules.value)
const disabled = computed(() => loading.value || busy.value || jobActive.value || !!loadError.value)
const stale = computed(() => !!run.value?.stale || rulesDirty.value)
const allFindings = computed(() => [...(run.value?.issues ?? []), ...(run.value?.uncertain ?? [])].sort((a, b) => a.start - b.start))
const findings = computed(() => allFindings.value.filter(i =>
  filter.value === 'all' || (filter.value === 'uncertain' ? i.kind === 'uncertain' : i.kind === 'conflict' && i.status === filter.value),
))
const selected = computed(() => allFindings.value.find(i => i.id === selectedId.value) ?? null)
const displayedContent = computed(() => run.value?.content_snapshot ?? savedContent.value)
const selection = computed(() => {
  const chars = Array.from(displayedContent.value)
  const issue = selected.value
  if (!issue || chars.slice(issue.start, issue.end).join('') !== issue.quote) return null
  return { before: chars.slice(0, issue.start).join(''), quote: issue.quote, after: chars.slice(issue.end).join('') }
})
const pendingCount = computed(() => run.value?.issues.filter(i => i.status === 'pending').length ?? 0)
const statusLabel: Record<ReviewStatus, string> = { pending: '待处理', confirmed: '已确认', ignored: '已忽略' }
const magicOptions = ['火系', '水系', '冰系', '雷系', '风系'].map(value => ({ label: value, value }))
const actionOptions = [
  { label: '用剑', value: 'use_sword' }, { label: '射箭', value: 'use_bow' }, { label: '开枪', value: 'use_gun' },
  ...magicOptions.map(x => ({ label: `使用${x.value}`, value: ({ 火系: 'fire_magic', 水系: 'water_magic', 冰系: 'ice_magic', 雷系: 'lightning_magic', 风系: 'wind_magic' } as Record<string, string>)[x.value]! })),
]
const filterOptions = [
  { label: '全部', value: 'all' }, { label: '待处理', value: 'pending' },
  { label: '已确认', value: 'confirmed' }, { label: '已忽略', value: 'ignored' }, { label: '待复核项', value: 'uncertain' },
]

async function load() {
  const sequence = ++loadSequence
  const key = routeKey.value
  loading.value = true
  loadError.value = ''
  notice.value = ''
  if (!Number.isInteger(number.value) || number.value < 1) {
    loadError.value = '章节编号无效，请返回工作台选择章节。'
    loading.value = false
    return
  }
  try {
    const [config, chapter, latest] = await Promise.all([
      evidenceReviewApi.rules(novel.value), chapterApi.getChapter(novel.value, number.value),
      typeof route.query.run === 'string' ? evidenceReviewApi.getRun(novel.value, route.query.run) : evidenceReviewApi.latest(novel.value, number.value, mode.value),
    ])
    if (sequence !== loadSequence || key !== routeKey.value) return
    rules.value = config
    savedRules.value = serialize(config)
    savedContent.value = chapter.content || ''
    title.value = chapter.title || `第 ${number.value} 章`
    applyRun(latest)
    const [status, history, active] = await Promise.allSettled([
      evidenceReviewApi.modelStatus(), evidenceReviewApi.history(novel.value, number.value),
      evidenceReviewApi.activeJob(novel.value, number.value),
    ])
    if (sequence !== loadSequence || key !== routeKey.value) return
    model.value = status.status === 'fulfilled' ? status.value : null
    runHistory.value = history.status === 'fulfilled' ? history.value : []
    job.value = active.status === 'fulfilled' ? active.value : null
    if (job.value) schedulePoll(job.value.id, key)
  } catch (error) {
    if (sequence === loadSequence) loadError.value = formatApiError(error, '加载失败，请确认作品和章节存在，再重新加载。')
  } finally {
    if (sequence === loadSequence) loading.value = false
  }
}

function refresh() {
  if (rulesDirty.value) {
    dialog.warning({
      title: '放弃未保存的设定？', content: '重新加载会使用服务端保存的设定。',
      positiveText: '放弃并加载', negativeText: '继续编辑', onPositiveClick: () => { void load() },
    })
  } else void load()
}

function applyRun(value: EvidenceRun | null) {
  run.value = value
  if (!allFindings.value.some(i => i.id === selectedId.value)) selectedId.value = allFindings.value[0]?.id ?? ''
}

function addProfile() {
  if (rules.value.profiles.length >= 64) return
  rules.value.profiles.push({ id: crypto.randomUUID(), name: '', aliases: [], allowed_magic: [], forbidden_actions: [], facts: [] })
}

async function importCharacters() {
  busy.value = true; notice.value = ''
  const key = routeKey.value
  try {
    const preview = await evidenceReviewApi.importCharacters(novel.value)
    if (key !== routeKey.value) return
    let added = 0
    for (const profile of preview.profiles) {
      const existing = rules.value.profiles.find(item => item.name === profile.name)
      if (existing) {
        existing.facts = [...new Set([...(existing.facts || []), ...(profile.facts || [])])].slice(0,24)
      } else if (rules.value.profiles.length < 64 && !rules.value.profiles.some(item => item.aliases.includes(profile.name))) {
        rules.value.profiles.push(profile); added++
      }
    }
    if (preview.warnings.length) notice.value = preview.warnings.join('；')
    if (!preview.profiles.length) message.info('创作人物库暂无可导入人物，请先在工作台人物设定中添加')
    else message.success(`已载入人物草稿，新增 ${added} 人；请核对并保存`)
  } catch(error) { if (key === routeKey.value) notice.value = formatApiError(error,'读取作品人物失败') }
  finally { busy.value = false }
}

function removeProfile(profile: CharacterRule) {
  dialog.warning({
    title: `移除${profile.name || '这位人物'}的审校设定？`, content: '保存后生效；已有审校记录会保留并标记为过期。',
    positiveText: '移除设定', negativeText: '保留',
    onPositiveClick: () => { rules.value.profiles = rules.value.profiles.filter(p => p.id !== profile.id) },
  })
}

async function saveRules() {
  const key = routeKey.value
  busy.value = true
  notice.value = ''
  try {
    const result = await evidenceReviewApi.saveRules(novel.value, rules.value)
    if (key !== routeKey.value) return
    rules.value = result
    savedRules.value = serialize(result)
    // Refresh the version verdict without discarding the manuscript snapshot.
    applyRun(await evidenceReviewApi.latest(novel.value, number.value, mode.value))
    message.success('人物设定已保存')
  } catch (error) {
    if (key === routeKey.value) notice.value = formatApiError(error, '保存失败，请重新加载后重试。')
  } finally { busy.value = false }
}

async function review() {
  const key = routeKey.value
  busy.value = true
  notice.value = ''
  try {
    const result = await evidenceReviewApi.run(novel.value, number.value, savedContent.value)
    if (key !== routeKey.value) return
    applyRun(result)
    await router.replace({ query: { ...route.query, mode: 'rules' } })
    runHistory.value = await evidenceReviewApi.history(novel.value, number.value)
    filter.value = 'all'
    message.success('审校完成')
  } catch (error) {
    if (key === routeKey.value) notice.value = formatApiError(error, '审校失败，请重新加载后重试。')
  } finally { busy.value = false }
}

async function feedback(issue: EvidenceIssue, status: ReviewStatus) {
  if (!run.value || stale.value) return
  const key = routeKey.value
  busy.value = true
  notice.value = ''
  try {
    const result = await evidenceReviewApi.feedback(novel.value, run.value.id, issue, status)
    if (key === routeKey.value) {
      applyRun(result)
      message.success(status === 'pending' ? '已撤销处理' : statusLabel[status])
    }
  } catch (error) {
    if (key === routeKey.value) notice.value = formatApiError(error, '处理失败，请重新加载检查最新状态。')
  } finally { busy.value = false }
}

async function locate(issue: EvidenceIssue) {
  selectedId.value = issue.id
  await nextTick()
  selectedMark.value?.scrollIntoView({ block: 'center', behavior: 'instant' })
  selectedMark.value?.focus({ preventScroll: true })
}

async function retrieveHistory() {
  const key = routeKey.value
  retrieving.value = true
  notice.value = ''
  try {
    const result = await evidenceReviewApi.retrieve(novel.value, number.value, retrievalQuery.value, false, retrievalMode.value)
    if (key === routeKey.value) { retrieval.value = result; panel.value = 'history' }
  } catch (error) { if (key === routeKey.value) notice.value = formatApiError(error, '历史检索失败') }
  finally { retrieving.value = false }
}

function onPanelKeyDown(event: KeyboardEvent) {
  const index = panels.findIndex(item => item.value === panel.value)
  let target = index
  if (event.key === 'ArrowRight') target = (index + 1) % panels.length
  else if (event.key === 'ArrowLeft') target = (index - 1 + panels.length) % panels.length
  else if (event.key === 'Home') target = 0
  else if (event.key === 'End') target = panels.length - 1
  else return
  event.preventDefault()
  panel.value = panels[target]!.value
  void nextTick(() => document.getElementById(`review-tab-${panel.value}`)?.focus())
}

async function startAI() {
  const key = routeKey.value
  busy.value = true
  notice.value = ''
  try {
    const result = await evidenceReviewApi.startAI(novel.value, number.value, savedContent.value, false, outputTokens.value, retrievalMode.value)
    if (key !== routeKey.value) return
    job.value = result
    await router.replace({ query: { ...route.query, mode: 'ai' } })
    schedulePoll(result.id, key)
  } catch (error) { if (key === routeKey.value) notice.value = formatApiError(error, 'AI 审校未能启动') }
  finally { busy.value = false }
}

function schedulePoll(id: string, key: string, failures = 0) {
  clearTimeout(pollTimer)
  pollTimer = setTimeout(async () => {
    if (key !== routeKey.value) return
    try {
      const result = await evidenceReviewApi.job(novel.value, id)
      if (key !== routeKey.value) return
      job.value = result
      if (result.status === 'queued' || result.status === 'running') { schedulePoll(id, key); return }
      if (result.status === 'completed' && result.run_id) {
        const [completed, config, history] = await Promise.all([
          evidenceReviewApi.getRun(novel.value, result.run_id), evidenceReviewApi.rules(novel.value),
          evidenceReviewApi.history(novel.value, number.value),
        ])
        if (key !== routeKey.value) return
        applyRun(completed)
        if (!rulesDirty.value) { rules.value = config; savedRules.value = serialize(config) }
        runHistory.value = history
        panel.value = 'results'
        message.success('AI 审校完成，引用已核对')
      } else if (result.status === 'failed' || result.status === 'interrupted') notice.value = result.error || '任务未完成，可在任务与用量中查看并重新发起'
    } catch (error) {
      if (key !== routeKey.value) return
      if (failures < 2) schedulePoll(id, key, failures + 1)
      else { job.value = null; notice.value = formatApiError(error, '暂时无法读取任务，请重新加载确认最新状态') }
    }
  }, 1000)
}

async function cancelAI() {
  if (!job.value) return
  try { job.value = await evidenceReviewApi.cancelJob(novel.value, job.value.id) }
  catch (error) { notice.value = formatApiError(error, '取消请求失败，请重新加载检查任务') }
}

async function selectHistoricalRun(id: string) {
  busy.value = true
  try {
    const result = await evidenceReviewApi.getRun(novel.value, id)
    applyRun(result)
    await router.replace({ query: { ...route.query, mode: result.mode } })
    panel.value = 'results'
  } catch (error) { notice.value = formatApiError(error, '读取运行记录失败') }
  finally { busy.value = false }
}

async function exportReport() {
  if (!run.value) return
  try {
    const current = await evidenceReviewApi.getRun(novel.value, run.value.id)
    const url = URL.createObjectURL(new Blob([JSON.stringify(current, null, 2)], { type: 'application/json' }))
    const anchor = document.createElement('a')
    anchor.href = url; anchor.download = `叙衡审校-${number.value}-${current.id.slice(0, 8)}.json`; anchor.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (error) { notice.value = formatApiError(error, '报告导出失败') }
}

watch(routeKey, () => { clearTimeout(pollTimer); job.value = null; retrieval.value = null; run.value = null; selectedId.value = ''; filter.value = 'all'; void load() }, { immediate: true })
onBeforeUnmount(() => { ++loadSequence; clearTimeout(pollTimer) })
function confirmLeave() {
  if (!rulesDirty.value || loading.value) return true
  return new Promise<boolean>(resolve => {
    dialog.warning({
      title: '人物设定尚未保存', content: '离开会放弃本次设定修改。',
      positiveText: '放弃并离开', negativeText: '继续编辑',
      onPositiveClick: () => resolve(true), onNegativeClick: () => resolve(false),
      onClose: () => resolve(false), onMaskClick: () => resolve(false),
    })
  })
}
onBeforeRouteLeave(confirmLeave)
onBeforeRouteUpdate((to, from) => to.params.id !== from.params.id || to.params.slug !== from.params.slug ? confirmLeave() : true)
</script>

<template>
 <div class="review-shell">
  <WorkspaceNav section="review" :novel-id="novel" :chapter-number="number" />
  <main class="review-desk">
    <header class="desk-topbar"><div class="desk-breadcrumb"><router-link to="/">作品库</router-link><AppIcon name="chevron" :size="13"/><span>一致性审校</span></div><div class="desk-top-actions"><span class="save-indicator"><i/>已保存正文</span><n-button quaternary :disabled="busy" @click="router.push(`/book/${novel}/workbench?chapter=${number}`)"><template #icon><AppIcon name="write" :size="15"/></template>返回创作</n-button></div></header>
    <div class="desk-content">
    <header class="desk-header"><div class="chapter-heading"><p class="eyebrow">第 {{ String(number).padStart(2,'0') }} 章 · 一致性审校</p><h1>{{ title || `第 ${number} 章` }}</h1><p class="desk-subtitle">对照正文与依据，把每一条提示核查清楚。</p></div><div class="desk-actions"><n-button :disabled="busy || loading" @click="settingsOpen = true"><template #icon><AppIcon name="settings" :size="15"/></template>审校设置<span v-if="rulesDirty" class="unsaved-dot"/></n-button><n-button secondary aria-label="审校已保存正文" :loading="busy && !jobActive" :disabled="disabled || rulesDirty || !savedContent.trim() || !rules.profiles.length" @click="review"><template #icon><AppIcon name="check" :size="16"/></template>规则审校</n-button><n-button type="primary" :disabled="disabled || rulesDirty || !savedContent.trim() || !model?.configured || !rules.profiles.length" @click="startAI"><template #icon><AppIcon name="sparkle" :size="16"/></template>AI 深度审校</n-button></div></header>
    <n-alert v-if="loadError" type="error" title="无法加载审校工作台" class="desk-alert">{{ loadError }}</n-alert>
    <n-alert v-if="notice" type="error" title="操作未完成" class="desk-alert" closable @close="notice = ''">{{ notice }}</n-alert>
    <n-alert v-if="jobActive" type="info" title="AI 审校进行中" class="desk-alert"><n-space align="center"><span>{{ job?.stage }}</span><n-button size="small" :disabled="job?.cancel_requested" @click="cancelAI">请求取消</n-button></n-space></n-alert>
    <n-alert v-if="run?.stale" type="warning" title="这份审校记录已过期" class="desk-alert">{{ run.stale_reasons.join('、') }}。下方保留当时的正文与依据；重新加载后再次审校。</n-alert>
    <n-alert v-else-if="rulesDirty" type="info" class="desk-alert">人物设定尚未保存，保存后才能审校和处理问题。</n-alert>
    <div class="review-overview"><div class="review-count"><span>待处理</span><strong>{{ run ? pendingCount : '—' }}</strong></div><div class="review-count"><span>已确认</span><strong>{{ run ? run.issues.filter(item => item.status === 'confirmed').length : '—' }}</strong></div><div class="review-count"><span>待复核</span><strong>{{ run ? run.uncertain.length : '—' }}</strong></div><div class="review-model"><span class="model-dot" :class="{ ready: model?.configured }"/><div><span>{{ model?.configured ? model.model : '尚未配置模型' }}</span><small>{{ run?.mode === 'ai' ? '当前展示 AI 审校结果' : '规则审校可离线使用' }}</small></div><button type="button" @click="refresh" aria-label="重新加载审校结果"><AppIcon name="history" :size="16"/></button></div></div>
    <n-spin :show="loading">
      <div v-if="!loadError" class="desk-layout">
        <section class="manuscript-column" aria-labelledby="manuscript-heading">
          <div class="column-heading"><div class="manuscript-label"><AppIcon name="book" :size="17"/><h2 id="manuscript-heading">章节原文</h2></div><span class="muted">{{ Array.from(displayedContent).length }} 字符<span class="read-only-label">只读</span></span></div>
          <div class="manuscript"><p class="prose-chapter-number">第 {{ String(number).padStart(2,'0') }} 章</p><p class="prose-title">{{ title }}</p><div class="manuscript-text"><template v-if="selection">{{ selection.before }}<mark ref="selectedMark" tabindex="-1" class="evidence-mark">{{ selection.quote }}</mark>{{ selection.after }}</template><template v-else>{{ displayedContent || '请先在章节页面保存正文，再回来审校。' }}</template></div></div>
          <footer class="manuscript-footer"><span><AppIcon name="check" :size="13"/>原文与依据逐字核对</span><span v-if="run">{{ new Date(run.created_at).toLocaleString('zh-CN', { hour12: false }) }}</span></footer>
        </section>
        <section class="findings-column" aria-labelledby="findings-heading" aria-live="polite">
          <div class="column-heading"><div><p class="eyebrow">逐项核对</p><h2 id="findings-heading">审校记录</h2></div><n-tag v-if="run" size="small" type="warning">{{ pendingCount }} 项待处理</n-tag></div>
          <div class="review-tabs" role="tablist" aria-label="审校工作区">
            <button v-for="item in panels" :key="item.value" :id="`review-tab-${item.value}`" type="button" role="tab" :aria-selected="panel === item.value" :aria-controls="`review-panel-${item.value}`" :tabindex="panel === item.value ? 0 : -1" @click="panel = item.value" @keydown="onPanelKeyDown">{{ item.label }}</button>
          </div>
          <div v-if="panel === 'history'" id="review-panel-history" role="tabpanel" aria-labelledby="review-tab-history" class="history-pane">
            <SearchIndexControls :novel="novel" />
            <n-input v-model:value="retrievalQuery" placeholder="人物、地点或情节；留空按当前正文检索" :input-props="{ 'aria-label': '历史检索内容' }" :maxlength="8000" />
            <n-button block secondary :loading="retrieving" :disabled="disabled || retrieving" @click="retrieveHistory">检索历史正文</n-button>
            <template v-if="retrieval">
              <p class="muted">{{ retrieval.indexed_chapters }} 章 · {{ retrieval.indexed_chunks }} 个片段 · {{ retrievalLabels[retrieval.mode] || retrieval.mode }}</p>
              <p v-for="warning in retrieval.warnings" :key="warning" class="muted">{{ warning }}</p>
              <article v-for="source in retrieval.sources" :key="source.id" class="source-card"><p class="eyebrow">第 {{ source.chapter_number }} 章 · 原文片段</p><blockquote>{{ source.text }}</blockquote><router-link :to="`/book/${novel}/chapter/${source.chapter_number}`" target="_blank">查看来源章节 ↗</router-link></article>
              <p class="muted">{{ retrieval.scope }}</p>
            </template>
          </div>
          <div v-else-if="panel === 'runs'" id="review-panel-runs" role="tabpanel" aria-labelledby="review-tab-runs" class="history-pane">
            <n-empty v-if="!runHistory.length" description="尚无运行记录" />
            <button v-for="item in runHistory" :key="item.id" class="finding" :disabled="disabled" @click="selectHistoricalRun(item.id)"><strong>{{ item.mode === 'ai' ? 'AI 深度审校' : '本地规则审校' }}</strong><span class="finding-description">{{ new Date(item.created_at).toLocaleString('zh-CN', { hour12: false }) }}</span><span class="muted">{{ item.model_name || '规则检查' }} · {{ Math.round(item.elapsed_ms) }} ms</span></button>
          </div>
          <div v-else id="review-panel-results" role="tabpanel" aria-labelledby="review-tab-results">
          <div v-if="!run" class="start-state"><div class="proof-symbol" aria-hidden="true">↔</div><h3>让每条提示都有依据</h3><p>先在审校设置中保存人物限制，再运行规则或 AI 审校，逐项核对原文与依据。</p><n-button secondary @click="settingsOpen = true">配置人物与模型</n-button></div>
          <template v-else>
            <n-space align="center" justify="space-between"><n-tag size="small">{{ run.mode === 'ai' ? 'AI 深度审校' : '本地规则' }}</n-tag><n-button text size="small" @click="exportReport">导出报告</n-button></n-space>
            <p v-if="run.summary" class="muted review-summary">{{ run.summary }}</p>
            <n-alert v-for="warning in run.warnings || []" :key="warning" type="warning" :show-icon="false" class="result-warning">{{ warning }}</n-alert>
            <n-select v-model:value="filter" :options="filterOptions" aria-label="筛选审校状态" size="small" />
            <p class="muted result-meta">{{ run.issues.length }} 项设定偏离 · {{ run.uncertain.length }} 项{{ run.mode === 'ai' ? '待复核项' : '待确认主体' }}</p>
            <n-empty v-if="!findings.length" :description="allFindings.length ? '此筛选下暂无记录' : '在当前检查范围内未发现问题'" class="finding-empty" />
            <button v-for="issue in findings" :key="issue.id" type="button" class="finding" :class="{ selected: selectedId === issue.id }" @click="locate(issue)">
              <span class="finding-top"><strong>{{ issue.entity_name || (issue.origin === 'ai' ? '叙述一致性' : '主体待确认') }}</strong><span class="status" :class="issue.kind === 'uncertain' ? 'uncertain' : issue.status">{{ issue.kind === 'uncertain' ? (issue.origin === 'ai' ? '待复核' : '待确认主体') : statusLabel[issue.status] }}</span></span>
              <q>{{ issue.quote }}</q><span class="finding-description">{{ issue.description }}</span><span class="locate-link">定位原文 ↗</span>
            </button>
            <section v-if="selected" class="source-card" aria-label="当前问题的设定依据">
              <p class="eyebrow">{{ stale ? '当时保存的依据' : selected.source_kind === 'history' ? `历史原文 · 第 ${selected.source_chapter_number} 章` : '设定依据' }}</p>
              <blockquote>{{ selected.source_quote || '行为主体尚不明确，无法对应到人物设定。' }}</blockquote>
              <p class="muted">{{ selected.kind === 'uncertain' ? '请结合上下文判断，不将此项计作确定冲突。' : '请判断是否属于剧情中的例外，再确认或忽略。' }}</p>
              <router-link v-if="selected.source_kind === 'history'" :to="`/book/${novel}/chapter/${selected.source_chapter_number}`" target="_blank">查看来源章节 ↗</router-link>
              <n-space v-if="selected.kind === 'conflict'">
                <template v-if="selected.status === 'pending'">
                  <n-button size="small" type="primary" :disabled="disabled || stale" @click="feedback(selected, 'confirmed')">确认问题</n-button>
                  <n-button size="small" :disabled="disabled || stale" @click="feedback(selected, 'ignored')">忽略此项</n-button>
                </template>
                <n-button v-else size="small" :disabled="disabled || stale" @click="feedback(selected, 'pending')">撤销处理</n-button>
              </n-space>
            </section>
            <p v-if="run.mode === 'ai'" class="muted result-meta">{{ run.model_name }} · {{ run.model_calls }} 次模型请求 · 输入 {{ run.input_tokens ?? '未返回' }} / 输出 {{ run.output_tokens ?? '未返回' }} Token</p>
          </template>
          </div>
        </section>

      </div>
    </n-spin>
    <footer class="desk-footer"><span>叙衡 · 一致性审校工作台</span><router-link to="/about">关于与许可</router-link></footer>
    </div>
  </main>
  <n-drawer v-model:show="settingsOpen" :width="440" placement="right" :close-on-esc="true" class="review-settings-drawer" :style="{ maxWidth: '100vw' }">
    <n-drawer-content title="审校设置" closable><aside class="rules-column">          <div class="column-heading"><div><p class="eyebrow">判断依据</p><h2>人物设定</h2></div><n-tag size="small" :type="rulesDirty ? 'warning' : 'default'">{{ rulesDirty ? '未保存' : '已同步' }}</n-tag></div>
          <p class="muted help">通用事实交给 AI 对照；动作限制用于本地规则。常用武器不等于禁止其他武器。可从创作人物库导入草稿，核对保存后生效。</p>
          <n-button block secondary :disabled="disabled" @click="importCharacters">从作品人物库导入草稿</n-button>
          <n-empty v-if="!rules.profiles.length" description="先添加需要检查的人物" class="rule-empty" />
          <fieldset v-for="(profile, index) in rules.profiles" :key="profile.id" class="profile" :disabled="disabled">
            <legend>{{ profile.name || `人物 ${index + 1}` }}</legend>
            <label :for="`name-${profile.id}`">人物名</label>
            <n-input :input-props="{ id: `name-${profile.id}` }" v-model:value="profile.name" placeholder="例如：林烬" :maxlength="40" :disabled="disabled" />
            <label>别名</label><n-dynamic-tags v-model:value="profile.aliases" :max="12" :disabled="disabled" />
            <label>通用事实（AI 审校）</label>
            <n-dynamic-tags v-model:value="profile.facts" :max="24" :disabled="disabled" class="fact-tags" :input-props="{maxlength:300,placeholder:'例如：左臂受伤，尚未康复'}" />
            <label>仅能使用的元素能力</label>
            <n-select v-model:value="profile.allowed_magic" multiple :options="magicOptions" placeholder="留空表示不限制" aria-label="仅能使用的元素能力" :disabled="disabled" />
            <label>明确禁止的行为</label>
            <n-select v-model:value="profile.forbidden_actions" multiple :options="actionOptions" placeholder="留空表示没有明确禁令" aria-label="明确禁止的行为" :disabled="disabled" />
            <n-button text type="error" size="small" @click="removeProfile(profile)">移除人物设定</n-button>
          </fieldset>
          <n-space vertical>
            <n-button dashed block :disabled="disabled || rules.profiles.length >= 64" @click="addProfile">＋ 添加人物</n-button>
            <n-button block secondary type="primary" :disabled="disabled || !rulesDirty" @click="saveRules">保存人物设定</n-button>
          </n-space>
          <p class="muted scope-note">规则模式检查元素能力与禁用行为；AI 模式还会对照人物事实和历史正文。事实变化可能是合理剧情，需要人工复核；未发现问题不代表全文没有矛盾。</p>
          <div class="ai-settings">
            <h3>AI 与检索</h3>
            <p class="muted">{{ model?.configured ? `当前模型：${model.model}` : '尚未配置模型。打开 AI 控制台填写并激活档案，再重新加载。' }}</p>
            <label>历史检索模式</label>
            <n-select v-model:value="retrievalMode" :disabled="disabled" :options="retrievalModes" aria-label="历史检索模式"/>
            <p class="muted">混合模式同时查找关键词与相近含义；重排模式进一步核对候选的相关性。首次建立索引会产生嵌入请求，之后复用缓存。</p>
            <RetrievalSettings/>
            <label>本次 AI 输出上限</label>
            <n-select v-model:value="outputTokens" :options="[2048, 4096, 8192].map(value => ({ value, label: `${value} Token` }))" :disabled="disabled" aria-label="本次 AI 输出上限" />
          </div>
</aside><template #footer><n-button type="primary" @click="settingsOpen = false">完成设置</n-button></template></n-drawer-content>
  </n-drawer>
 </div>
</template>
<style scoped>
.fact-tags :deep(.n-tag){height:auto;min-height:28px;max-width:100%;padding-top:4px;padding-bottom:4px}.fact-tags :deep(.n-tag__content){white-space:normal;overflow-wrap:anywhere}
.review-shell{--review-proof:#27847e;
  --review-highlight:#fff0bc;
  --review-ink:var(--app-text-primary);
  --review-muted:var(--app-text-muted);
  --review-line:var(--app-border);
  --review-surface:var(--app-surface);
  --review-canvas:var(--app-page-bg);
  background:var(--app-page-bg);
  color:var(--app-text-primary);
  min-height:100vh}
.review-desk{margin-left:216px;
  min-width:0}
.desk-topbar{height:65px;
  background:var(--app-surface);
  border-bottom:1px solid var(--app-border);
  display:flex;
  align-items:center;
  justify-content:space-between;
  padding:0 30px;
  gap:12px}
.desk-breadcrumb{display:flex;
  align-items:center;
  gap:12px;
  font-size:11px;
  color:var(--app-text-muted)}
.desk-breadcrumb a{text-decoration:none;
  color:inherit}
.desk-breadcrumb>span{color:var(--app-text-secondary)}
.desk-top-actions{display:flex;
  align-items:center;
  gap:16px}
.save-indicator{display:flex;
  align-items:center;
  gap:6px;
  font-size:10px;
  color:var(--app-text-muted)}
.save-indicator i,.model-dot{height:5px;
  width:5px;
  border-radius:50%;
  background:var(--app-text-muted);
  flex-shrink:0}
.save-indicator i,.model-dot.ready{background:#3c9182}
.desk-content{max-width:1660px;
  margin:0 auto;
  padding:30px 30px 18px}
.desk-header{display:flex;
  align-items:center;
  justify-content:space-between;
  gap:18px;
  margin-bottom:24px}
.eyebrow{font-size:10px;
  color:var(--app-text-muted);
  letter-spacing:.04em;
  margin:0 0 7px}
.chapter-heading h1{font-size:24px;
  font-weight:600;
  letter-spacing:-.025em;
  margin:0 0 8px}
.desk-subtitle{font-size:11px;
  color:var(--app-text-muted);
  margin:0}
.desk-actions{display:flex;
  gap:8px;
  flex-wrap:wrap;
  justify-content:flex-end}
.desk-actions :deep(.n-button){height:34px;
  font-size:11px}
.unsaved-dot{width:5px;
  height:5px;
  background:#c78e32;
  border-radius:50%;
  margin-left:7px}
.desk-alert{margin-bottom:16px}
.review-overview{display:flex;
  border:1px solid var(--app-border);
  border-radius:8px;
  background:var(--app-surface);
  padding:16px 20px;
  margin-bottom:22px;
  align-items:center;
  gap:28px}
.review-count{display:flex;
  align-items:center;
  gap:12px;
  min-width:100px;
  border-right:1px solid var(--app-border);
  padding-right:28px}
.review-count span{font-size:11px;
  color:var(--app-text-muted)}
.review-count strong{font-size:21px;
  font-weight:550;
  font-variant-numeric:tabular-nums}
.review-model{display:flex;
  align-items:center;
  gap:9px;
  margin-left:auto}
.review-model>div>span{font-size:11px}
.review-model small{display:block;
  font-size:9px;
  color:var(--app-text-muted);
  margin-top:2px}
.review-model button{display:grid;
  place-items:center;
  background:none;
  border:0;
  color:var(--app-text-muted);
  cursor:pointer;
  padding:7px}
.desk-layout{display:grid;
  grid-template-columns:minmax(0,1fr) 352px;
  gap:22px;
  align-items:start}
.manuscript-column,.findings-column{min-width:0;
  border:1px solid var(--app-border);
  border-radius:9px;
  background:var(--app-surface);
  overflow:hidden}
.column-heading{display:flex;
  align-items:center;
  justify-content:space-between;
  gap:12px}
.column-heading h2{font-size:13px;
  font-weight:600;
  margin:0}
.manuscript-column>.column-heading{padding:17px 22px;
  border-bottom:1px solid var(--app-divider)}
.manuscript-label{display:flex;
  align-items:center;
  gap:9px}
.manuscript-label>svg{color:var(--app-text-muted)}
.read-only-label{display:inline-block;
  margin-left:12px;
  font-size:9px;
  border:1px solid var(--app-border);
  border-radius:3px;
  padding:0 5px}
.muted{font-size:11px;
  color:var(--app-text-muted)}
.manuscript{padding:34px 42px;
  min-height:530px;
  max-height:calc(100vh - 337px);
  overflow:auto;
  scrollbar-width:thin}
.prose-chapter-number{font-size:10px;
  letter-spacing:.18em;
  text-align:center;
  color:var(--app-text-muted);
  margin:0 0 8px}
.prose-title{text-align:center;
  font:600 22px/1.8 'STSong','SimSun',serif;
  margin:0 0 32px}
.manuscript-text{white-space:pre-wrap;
  overflow-wrap:anywhere;
  font:16px/2.25 'Noto Serif SC','STSong','SimSun',serif;
  color:var(--app-text-primary)}
.evidence-mark{background:#fff0bc;
  color:#654e20;
  border-bottom:2px solid #d9ad52;
  padding:3px 0;
  scroll-margin:100px}
.evidence-mark:focus-visible{outline:2px solid #27847e;
  outline-offset:3px}
.manuscript-footer{padding:12px 20px;
  border-top:1px solid var(--app-divider);
  font-size:9px;
  color:var(--app-text-muted);
  display:flex;
  justify-content:space-between;
  gap:12px;
  flex-wrap:wrap}
.manuscript-footer>span:first-child{display:flex;
  align-items:center;
  gap:5px}
.findings-column{padding:19px 18px}
.findings-column>.column-heading{margin-bottom:13px}
.findings-column>.column-heading .eyebrow{display:none}
.review-tabs{display:flex;
  gap:23px;
  border-bottom:1px solid var(--app-border);
  margin-bottom:16px}
.review-tabs button{padding:11px 0;
  border:0;
  border-bottom:2px solid transparent;
  color:var(--app-text-muted);
  background:none;
  font:11px var(--font-sans);
  cursor:pointer}
.review-tabs button[aria-selected=true]{color:var(--color-brand);
  border-bottom-color:var(--color-brand)}
.review-summary{font-size:11px;
  line-height:1.9;
  margin:14px 0;
  color:var(--app-text-muted)}
.result-meta{font-size:10px;
  line-height:1.8;
  margin:12px 0 16px}
.finding{display:block;
  width:100%;
  text-align:left;
  cursor:pointer;
  border:1px solid var(--app-border);
  border-left:3px solid transparent;
  border-radius:6px;
  background:var(--app-surface);
  color:inherit;
  padding:13px 12px;
  margin-bottom:10px;
  transition:background .15s,border-color .15s}
.finding:hover{background:var(--app-surface-subtle)}
.finding.selected{border-color:#b4d8d2;
  border-left-color:#27847e;
  background:color-mix(in srgb,#27847e 4%,var(--app-surface))}
.finding-top{display:flex;
  justify-content:space-between;
  align-items:center;
  gap:12px;
  font-size:12px;
  margin-bottom:8px}
.finding-top strong{font-weight:550}
.status{font-size:9px;
  border-radius:3px;
  padding:2px 5px;
  white-space:nowrap;
  background:var(--app-surface-subtle);
  color:var(--app-text-muted)}
.status.pending{color:#a36e1e;
  background:#fff3d8}
.status.confirmed{color:#287b6d;
  background:#e8f4ef}
.status.uncertain{color:#7761a6;
  background:#f1ecf9}
.finding q{display:block;
  font:14px/1.9 'STSong','SimSun',serif;
  margin:6px 0;
  overflow-wrap:anywhere}
.finding-description{display:block;
  color:var(--app-text-muted);
  font-size:10px;
  line-height:1.85}
.locate-link{display:block;
  color:#27847e;
  font-size:9px;
  margin-top:10px}
.source-card{margin-top:20px;
  border-top:1px solid var(--app-border);
  padding-top:16px}
.source-card>.eyebrow{color:#27847e;
  font-weight:500}
.source-card blockquote{margin:10px 0;
  padding:10px 12px;
  border-left:2px solid #6da69e;
  background:var(--app-surface-subtle);
  font:14px/1.9 'STSong','SimSun',serif;
  overflow-wrap:anywhere}
.source-card>.muted{font-size:10px;
  line-height:1.85;
  margin:9px 0}
.source-card>a{display:block;
  text-decoration:none;
  color:var(--color-brand);
  font-size:10px;
  margin:8px 0}
.source-card :deep(.n-button){font-size:10px}
.history-pane{display:grid;
  gap:12px}
.history-pane>.source-card{margin-top:8px}
.result-warning{margin:12px 0;
  font-size:11px}
.start-state{text-align:center;
  padding:48px 10px}
.proof-symbol{font-size:40px;
  color:#27847e;
  margin-bottom:12px}
.start-state h3{font-size:15px;
  font-weight:550;
  margin:10px 0}
.start-state p{font-size:11px;
  color:var(--app-text-muted);
  line-height:1.9;
  margin:0 0 20px}
.finding-empty{padding:30px 0}
.desk-footer{display:flex;
  justify-content:space-between;
  gap:15px;
  font-size:9px;
  color:var(--app-text-muted);
  margin-top:21px}
.desk-footer a{text-decoration:none;
  color:inherit}
.rules-column{color:var(--app-text-primary)}
.rules-column>.column-heading{margin-bottom:14px}
.help,.scope-note{line-height:1.9;
  font-size:11px;
  margin:12px 0 20px}
.profile{border:1px solid var(--app-border);
  border-radius:8px;
  padding:14px;
  margin:0 0 20px;
  min-width:0}
.profile legend{color:var(--color-brand);
  font-size:12px;
  font-weight:550;
  padding:0 6px}
.profile label{display:block;
  margin:12px 0 7px;
  font-size:11px;
  color:var(--app-text-muted)}
.profile :deep(.n-button){margin-top:14px}
.rule-empty{padding:24px}
.ai-settings{border-top:1px solid var(--app-border);
  padding-top:20px;
  margin-top:20px}
.ai-settings h3{font-size:13px;
  font-weight:550}
.ai-settings p{font-size:11px;
  line-height:1.9;
  margin:12px 0}
.ai-settings label{display:block;
  font-size:11px;
  margin:16px 0 8px}
button:focus-visible,a:focus-visible{outline:2px solid var(--color-brand);
  outline-offset:3px}

@media(min-width:1700px){.desk-layout{grid-template-columns:minmax(0,1fr) 390px}
.manuscript{padding:40px 65px}
.desk-content{padding:36px 42px}
}
@media(max-width:1220px){.desk-content{padding:25px 22px}
.desk-layout{grid-template-columns:minmax(0,1fr) 310px;
  gap:16px}
.desk-header{align-items:flex-start;
  flex-direction:column;
  gap:16px}
.desk-actions{justify-content:flex-start}
.review-overview{gap:17px;
  padding:14px 16px}
.review-count{min-width:0;
  padding-right:17px;
  gap:8px}
.manuscript{padding:30px 26px}
.review-model small{display:none}
.review-model>div>span{font-size:10px}
}
@media(max-width:1000px){.desk-layout{grid-template-columns:minmax(0,1fr)}
.manuscript{max-height:65vh;
  min-height:360px}
.review-model{gap:6px}
.review-model button{display:none}
}
@media(max-width:800px){.review-desk{margin-left:0}
.desk-topbar{padding:0 16px 0 66px}
.desk-top-actions .save-indicator{display:none}
.desk-top-actions :deep(.n-button){font-size:10px}
.desk-content{padding:24px 18px}
.chapter-heading h1{font-size:22px}
.desk-actions{gap:7px}
.desk-actions :deep(.n-button){font-size:10px;
  padding:0 10px}
.review-overview{gap:13px;
  margin-bottom:18px}
.review-count{padding-right:13px;
  gap:8px}
.review-count strong{font-size:18px}
.review-count span{font-size:10px}
.review-model{display:none}
.manuscript{min-height:330px;
  padding:25px 23px;
  max-height:63vh}
.manuscript-text{font-size:16px;
  line-height:2.1}
.prose-title{font-size:21px;
  margin-bottom:25px}
.manuscript-footer{font-size:8px;
  padding:12px 16px}
.findings-column{padding:19px 17px}
.desk-footer{font-size:8px}
}

@media(prefers-reduced-motion:reduce){.finding{transition:none}
}


</style>
