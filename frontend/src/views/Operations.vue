<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, onBeforeRouteLeave } from 'vue-router'
import { useDialog, useMessage } from 'naive-ui'
import WorkspaceNav from '@/components/layout/WorkspaceNav.vue'
import DataBackups from '@/components/operations/DataBackups.vue'
import AppIcon from '@/components/layout/AppIcon.vue'
import { operationsApi, type Operations, type UsagePolicy, type ModelCall, type Reconciliation, type AftermathTask } from '@/api/operations'
import { evidenceReviewApi, type ReviewJob } from '@/api/evidenceReview'
import { formatApiError } from '@/utils/apiError'

const route = useRoute()
const novel = computed(() => String(route.params.slug))
const dialog = useDialog()
const message = useMessage()
const data = ref<Operations | null>(null)
const error = ref('')
const saving = ref(false)
const pending = ref('')
const draft = ref<UsagePolicy>({ max_calls: null, max_tokens: null, max_cost: null, prices: [], timezone: 'Asia/Shanghai', daily_max_calls: null, daily_max_tokens: null, daily_max_cost: null })
const reportDay = ref('')
const offset = ref(0)
const uncertainOnly = ref(false)
const reconciliationCall = ref<ModelCall | null>(null)
const reconciliationOpen = ref(false)
const reconciliationHistory = ref<Reconciliation[]>([])
const reconciliationLoading = ref(false)
const reconciliationError = ref('')
const reconciling = ref(false)
const reconciliationBook = ref('')
const reconcileDraft = ref({input_tokens: null as number | null, output_tokens: null as number | null,
  billed_cost: null as number | null, note: '', reference: '', request_finished: false})
const reconciliationBaseline = ref('')
const reconciliationKey = ref('')
const aftermathOpen = ref(false)
const aftermathTask = ref<AftermathTask | null>(null)
const aftermathNote = ref('')
const aftermathRetryModels = ref(false)
const aftermathSaving = ref(false)
const aftermathError = ref('')
const aftermathBook = ref('')
const aftermathStatuses: Record<string,string> = {queued:'等待处理',running:'处理中',completed:'已完成',failed:'待恢复',waiting_confirmation:'需要确认',stale:'正文已变化'}
const reconciliationDirty = computed(() => reconciliationOpen.value && JSON.stringify(reconcileDraft.value) !== reconciliationBaseline.value)
const reconciliationValid = computed(() => Number.isSafeInteger(reconcileDraft.value.input_tokens) && Number.isSafeInteger(reconcileDraft.value.output_tokens)
  && (reconcileDraft.value.input_tokens ?? -1) >= 0 && (reconcileDraft.value.output_tokens ?? -1) >= 0
  && !!reconcileDraft.value.note.trim() && reconcileDraft.value.request_finished)
const tokenDifference = computed(() => {
  const call = reconciliationCall.value
  if (!call || reconcileDraft.value.input_tokens === null || reconcileDraft.value.output_tokens === null) return null
  const before = call.input_tokens === null || call.output_tokens === null ? call.reserved_tokens : call.input_tokens + call.output_tokens
  return reconcileDraft.value.input_tokens + reconcileDraft.value.output_tokens - before
})
const saved = ref('')
const dirty = computed(() => saved.value !== '' && JSON.stringify(draft.value) !== saved.value)
const policyOpen = ref(false)
const backupsOpen = ref(false)
const clock = ref(Date.now())
let timer: ReturnType<typeof setTimeout> | undefined
let sequence = 0
let disposed = false
const statuses: Record<string,string> = { queued: '排队中', running: '进行中', completed: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断' }
const kinds: Record<string,string> = { llm: '大模型', embedding: 'Embedding', rerank: 'Reranker' }
const kindOptions = Object.entries(kinds).map(([value,label]) => ({value,label}))
const checkpointLabels: Record<string,string> = {saving:'保存前确认',saved:'正文已保存',auditing:'章后审计',audit_report:'审计报告',aftermath_report:'章后提取',aftermath:'叙事同步',audited:'审计完成'}
const format = (n: number) => n.toLocaleString('zh-CN')
const signed = (n: number) => `${n > 0 ? '+' : ''}${format(n)}`
const time = (s: string) => new Date(s).toLocaleString('zh-CN', { hour12: false })
const active = (job: ReviewJob) => ['queued','running'].includes(job.status)
function age(job: ReviewJob) {
  const seconds = Math.max(0, Math.floor((clock.value - Date.parse(job.updated_at))/1000))
  return seconds < 60 ? `${seconds} 秒前` : `${Math.floor(seconds/60)} 分钟前`
}
async function load(initial = false) {
  const id = ++sequence
  clearTimeout(timer)
  try {
    const params = {...(reportDay.value ? {day: reportDay.value} : {}), ...(offset.value ? {offset: offset.value} : {}), ...(uncertainOnly.value ? {uncertain_only: true} : {})}
    const result = await operationsApi.get(novel.value, params)
    if (id !== sequence || disposed) return
    data.value = result; error.value = ''; clock.value = Date.now()
    if (initial) { draft.value = structuredClone(result.policy); saved.value = JSON.stringify(result.policy) }
  } catch (e) { if (id === sequence && !disposed) error.value = formatApiError(e, '读取任务记录失败，请重试') }
  finally { if (id === sequence && !disposed) timer = setTimeout(() => void load(), 5000) }
}
function openAftermath(task: AftermathTask) {
  aftermathTask.value = {...task}; aftermathBook.value = novel.value
  aftermathNote.value = ''; aftermathRetryModels.value = false; aftermathError.value = ''; aftermathOpen.value = true
}
async function resumeAftermath() {
  if (!aftermathTask.value || aftermathBook.value !== novel.value) return
  aftermathSaving.value = true; aftermathError.value = ''
  try {
    await operationsApi.resumeAftermath(aftermathBook.value, aftermathTask.value.id, {
      confirm_unknown: !!aftermathTask.value.unknown_requests && aftermathRetryModels.value,
      retry_model_calls: !aftermathTask.value.unknown_requests && aftermathRetryModels.value, note: aftermathNote.value,
    })
    aftermathOpen.value = false; message.success('章后任务已加入恢复队列，已完成步骤将复用'); await load()
  } catch (e) { aftermathError.value = formatApiError(e, '恢复失败，处理说明已保留') }
  finally { aftermathSaving.value = false }
}
async function openReconciliation(call: ModelCall) {
  reconciliationCall.value = {...call}
  reconciliationBook.value = novel.value
  reconcileDraft.value = {input_tokens: call.input_tokens, output_tokens: call.output_tokens, billed_cost: null, note: '', reference: '', request_finished: false}
  reconciliationBaseline.value = JSON.stringify(reconcileDraft.value)
  reconciliationKey.value = crypto.randomUUID()
  reconciliationHistory.value = []; reconciliationError.value = ''; reconciliationOpen.value = true
  reconciliationLoading.value = true
  const key = reconciliationKey.value
  try {
    const history = await operationsApi.reconciliations(novel.value, call.id)
    if (key === reconciliationKey.value) reconciliationHistory.value = history
  } catch (e) { if (key === reconciliationKey.value) reconciliationError.value = formatApiError(e, '读取核对历史失败') }
  finally { if (key === reconciliationKey.value) reconciliationLoading.value = false }
}
async function reconcile() {
  if (!reconciliationValid.value || !reconciliationCall.value || reconciliationBook.value !== novel.value) return
  reconciling.value = true
  try {
    await operationsApi.reconcile(reconciliationBook.value, reconciliationCall.value.id, {
      ...reconcileDraft.value, input_tokens: reconcileDraft.value.input_tokens!, output_tokens: reconcileDraft.value.output_tokens!,
      expected_revision: reconciliationCall.value.revision, request_key: reconciliationKey.value, request_finished: true,
    })
    reconciliationOpen.value = false; offset.value = 0
    message.success('账单核对已保存，预算已按核对结果更新'); await load()
  } catch (e) { reconciliationError.value = formatApiError(e, '保存核对失败，输入已保留') }
  finally { reconciling.value = false }
}
function closeReconciliation() {
  if (reconciling.value) return
  if (!reconciliationDirty.value) { reconciliationOpen.value = false; return }
  dialog.warning({title: '账单核对尚未保存', content: '离开将放弃本次核对输入。', positiveText: '放弃修改', negativeText: '继续编辑', onPositiveClick: () => { reconciliationOpen.value = false }})
}
async function save() {
  saving.value = true
  try {
    const result = await operationsApi.savePolicy(novel.value, draft.value)
    draft.value = structuredClone(result); saved.value = JSON.stringify(result)
    message.success('预算已保存，下次模型请求前生效'); await load()
  } catch (e) { message.error(formatApiError(e, '保存预算失败')) }
  finally { saving.value = false }
}
async function cancel(job: ReviewJob) {
  pending.value = job.id
  try { await evidenceReviewApi.cancelJob(novel.value, job.id); await load() }
  catch (e) { message.error(formatApiError(e, '取消任务失败')) }
  finally { pending.value = '' }
}
async function downloadDraft(number:number,hash:string) {
  try {
    const blob=await operationsApi.draft(novel.value,number,hash)
    const url=URL.createObjectURL(blob)
    const link=document.createElement('a');link.href=url;link.download=`chapter-${number}-recovered.md`
    document.body.appendChild(link);link.click();link.remove()
    setTimeout(()=>URL.revokeObjectURL(url),1000)
  } catch(e) { message.error(formatApiError(e,'取回生成稿失败')) }
}
function retry(job: ReviewJob) {
  dialog.warning({ title: '重新发起本章审校', content: '将按最新设定重新执行，并可能产生新的模型费用。原任务与已保存结果会保留；正文已变化时，需要回到章节重新审校。', positiveText: '确认重新发起', negativeText: '取消',
    onPositiveClick: async () => {
      pending.value = job.id
      try { await operationsApi.retry(novel.value, job.id); message.success('任务记录已更新'); await load() }
      catch (e) { message.error(formatApiError(e, '重新发起失败')) }
      finally { pending.value = '' }
    },
  })
}
onBeforeRouteLeave(() => {
  if (reconciling.value || aftermathSaving.value) return false
  if (!dirty.value && !reconciliationDirty.value) return true
  return new Promise<boolean>(resolve => dialog.warning({ title: dirty.value ? '预算尚未保存' : '账单核对尚未保存', content: '离开将放弃本次未保存修改。', positiveText: '放弃修改', negativeText: '继续编辑', onPositiveClick: () => resolve(true), onNegativeClick: () => resolve(false), onClose: () => resolve(false), onMaskClick: () => resolve(false) }))
})
watch(novel, () => { data.value = null; saved.value = ''; reportDay.value = ''; offset.value = 0; reconciliationOpen.value = false; aftermathOpen.value = false; policyOpen.value = false; reconciliationKey.value = ''; void load(true) }, { immediate: true })
onBeforeUnmount(() => { disposed = true; ++sequence; clearTimeout(timer) })
</script>

<template>
  <div class="operations-shell">
    <WorkspaceNav section="operations" :novel-id="novel" />
    <main class="operations-desk">
      <div class="operations-crumb"><router-link :to="`/book/${novel}/workbench`">章节创作</router-link><span>/</span><span>任务与用量</span></div>
      <DataBackups v-model:show="backupsOpen" />
      <header class="operations-header"><div><p class="operations-kicker">创作记录</p><h1>任务与用量</h1><p>查看执行进度，保留每一次请求的记录。</p></div><div class="operations-actions"><n-button @click="backupsOpen = true">数据备份</n-button><n-button @click="load()"><template #icon><AppIcon name="history" /></template>刷新记录</n-button><n-button type="primary" :disabled="!data" @click="policyOpen = true">设置作品预算</n-button></div></header>
      <n-alert v-if="error" type="error" title="任务记录暂时不可用" class="operations-alert">{{ error }}<n-button text @click="load(!data)">重新读取</n-button></n-alert>
      <template v-if="data">
        <n-alert v-if="data.budget_block" type="warning" title="作品预算已拦截请求" class="operations-alert">{{ data.budget_block.message }}。调整并保存预算后，请手动恢复托管或重新发起任务。</n-alert>
        <section class="usage-strip" aria-label="作品用量汇总">
          <div><span>模型请求</span><strong>{{ format(data.totals.calls) }}<small> 次</small></strong><p>{{ data.policy.max_calls === null ? '未设次数上限' : `作品上限 ${format(data.policy.max_calls)} 次` }}</p></div>
          <div><span>预算计入 Token</span><strong>{{ format(data.totals.charged_tokens) }}</strong><p>{{ data.totals.uncertain_calls ? `${data.totals.uncertain_calls} 次含预留用量` : '按服务商返回用量累计' }}</p></div>
          <div><span>累计费用估算 · 人民币</span><strong><small>¥ </small>{{ data.totals.estimated_cost.toFixed(4) }}</strong><p>{{ data.totals.unknown_cost_calls ? `${data.totals.unknown_cost_calls} 次费用未知，未计入此金额` : '按配置单价计算，包含在途预留' }}</p></div>
          <div><span>金额预算</span><strong>{{ data.policy.max_cost === null ? '未设置' : `¥ ${data.policy.max_cost}` }}</strong><p>超出预计预算时阻止下一次请求</p></div>
        </section>
        <p class="usage-note">仅记录本版本启用后、已关联本作品的模型请求。未返回用量或中断的请求保留预留值；估算不等同于服务商账单，无作品归属的连通性测试不计入。</p>
        <section class="operations-section daily-ledger" aria-label="每日用量">
          <div class="section-heading"><div><h2>每日用量</h2><p>{{ data.daily.timezone }} · {{ data.daily.date }}，按请求发起时间归属日期，跨日完成仍计入发起日。</p></div><div class="report-controls"><label>账目日期 <input v-model="reportDay" type="date" aria-label="账目日期" @change="load()" /></label><n-button size="small" :disabled="!reportDay" @click="reportDay = ''; load()">回到今天</n-button></div></div>
          <dl class="daily-summary">
            <div><dt>当日请求</dt><dd>{{ format(data.daily.totals.calls) }} <small>/ {{ data.policy.daily_max_calls ?? '不限' }} 次</small></dd></div>
            <div><dt>当日预算计入 Token</dt><dd>{{ format(data.daily.totals.charged_tokens) }} <small>/ {{ data.policy.daily_max_tokens === null ? '不限' : format(data.policy.daily_max_tokens) }}</small></dd></div>
            <div><dt>当日费用估算 · 人民币</dt><dd>¥ {{ data.daily.totals.estimated_cost.toFixed(4) }} <small>/ {{ data.policy.daily_max_cost === null ? '不限' : `¥ ${data.policy.daily_max_cost}` }}</small></dd></div>
          </dl>
          <p class="daily-note">{{ data.daily.totals.uncertain_calls }} 次含预留用量 · {{ data.daily.totals.unknown_cost_calls }} 次费用未知 · {{ data.daily.totals.reconciled_calls }} 次已核对。每日额度重置后，停止的任务仍需手动恢复。</p>
          <div v-if="data.daily.models.length" class="usage-table-scroll"><table class="usage-table daily-models"><thead><tr><th>服务 / 模型</th><th>请求次数</th><th>预算计入 Token</th><th>费用估算</th></tr></thead><tbody><tr v-for="model in data.daily.models" :key="`${model.kind}-${model.model}`"><td>{{ model.model }}<small>{{ kinds[model.kind] }}</small></td><td>{{ format(model.calls) }}</td><td>{{ format(model.charged_tokens) }}<small v-if="model.uncertain_calls">{{ model.uncertain_calls }} 次含预留</small></td><td>¥ {{ model.estimated_cost.toFixed(6) }}<small v-if="model.unknown_cost_calls">{{ model.unknown_cost_calls }} 次费用未知</small></td></tr></tbody></table></div>
          <p v-else class="table-empty">此日期还没有模型请求记录。</p>
        </section>
        <section class="operations-section">
          <div class="section-heading"><div><h2>审校任务</h2><p>每 5 秒更新。重新发起需要确认，不会自动重复请求。</p></div><span>{{ data.jobs.length }} 条记录</span></div>
          <div v-if="!data.jobs.length" class="operations-empty"><AppIcon name="review" :size="28" /><h3>还没有审校任务</h3><p>在章节中发起 AI 深度审校后，可在这里查看进度和结果。</p><router-link :to="`/book/${novel}/workbench`">返回章节创作 →</router-link></div>
          <article v-for="job in data.jobs" :key="job.id" class="job-row">
            <span class="job-status" :class="job.status">{{ statuses[job.status] }}</span>
            <div class="job-body"><h3>第 {{ job.number }} 章 · AI 一致性审校</h3><p>{{ job.stage }}</p><p v-if="job.error" class="job-error">{{ job.error }}</p><small>{{ time(job.created_at) }}<template v-if="active(job)"> · 执行心跳 {{ age(job) }}<span v-if="clock - Date.parse(job.updated_at) > 20000">，执行进程可能失去响应</span></template><template v-if="job.retry_of"> · 重新发起</template></small></div>
            <div class="job-actions"><router-link v-if="job.status === 'completed'" :to="`/book/${novel}/review/${job.number}?mode=ai&run=${job.run_id}`">查看结果 →</router-link><n-button v-else-if="active(job)" size="small" :disabled="job.cancel_requested || pending === job.id" @click="cancel(job)">请求取消</n-button><n-button v-else size="small" :disabled="!!pending" @click="retry(job)">重新发起</n-button></div>
          </article>
        </section>
        <section class="operations-section aftermath-section">
          <div class="section-heading"><div><h2>章后处理任务</h2><p>按正文版本留存叙事、图谱与辅助处理。重启后继续已排队任务，已保存的模型结果与写入不会重复执行。</p></div></div>
          <p v-if="!data.aftermath_tasks?.length" class="table-empty">暂无章后处理任务。保存正文或托管收稿后会在这里记录。</p>
          <article v-for="task in data.aftermath_tasks" :key="task.id" class="job-row aftermath-task">
            <span class="job-status" :class="task.status">{{ task.stale ? '旧版本' : aftermathStatuses[task.status] || task.status }}</span>
            <div class="job-body"><h3>第 {{ task.number }} 章 · {{ task.phase === 'core' ? '叙事与知识同步' : '章后辅助处理' }}</h3><p v-if="task.stale">正文已修改或删除，此任务不会继续写入，请按当前正文重新处理。</p><p v-else-if="task.error" class="job-error">{{ task.error }}</p><p v-if="task.unknown_requests && !task.stale" class="job-error">{{ task.unknown_requests }} 个请求的结果未能确认，可能已经计费；不会自动重新请求。</p><small>已留存 {{ task.steps.filter(s => s.startsWith('stage:')).length }} 个阶段 · {{ time(task.updated_at) }}</small><details v-if="task.confirmations.length" class="aftermath-confirmations"><summary>恢复处理记录（{{ task.confirmations.length }}）</summary><p v-for="(record,index) in task.confirmations" :key="index">{{ time(record.created_at) }} · {{ record.confirmed_unknown ? '已确认未留存请求的处理' : '复用已有结果继续' }}<br />{{ record.note }}</p></details></div>
            <div class="job-actions"><n-button v-if="!task.stale && ['failed','waiting_confirmation'].includes(task.status)" size="small" @click="openAftermath(task)">处理并恢复</n-button><span v-else-if="['queued','running'].includes(task.status) && !task.stale" class="budget-help">后台处理中</span></div>
          </article>
        </section>
        <section class="operations-section">
          <div class="section-heading"><div><h2>托管章节检查点</h2><p>保存正文与章后步骤的完成记录。中断后从创作工作台手动恢复，已完成步骤按正文版本复用。</p></div></div>
          <p v-if="!data.chapter_progress?.length" class="table-empty">暂无章节检查点。新版托管保存正文后会在这里记录。</p>
          <article v-for="checkpoint in data.chapter_progress" :key="`${checkpoint.number}-${checkpoint.content_hash}`" class="job-row">
            <span class="job-status" :class="checkpoint.stale ? '' : checkpoint.status">{{ checkpoint.uncommitted ? '待确认' : checkpoint.stale ? '旧版本' : checkpoint.status === 'completed' ? '已完成' : checkpoint.status === 'failed' ? '待恢复' : '未完成' }}</span>
            <div class="job-body"><h3>第 {{ checkpoint.number }} 章 · {{ checkpointLabels[checkpoint.stage] || '章后处理' }}</h3><p v-if="checkpoint.uncommitted">生成稿与数据库正文尚未一致，请先取回稿件并确认正文，再恢复托管。</p><p v-else-if="checkpoint.stale">正文已修改或章节已删除，此记录不会作为当前正文的恢复依据。</p><p v-else-if="checkpoint.error" class="job-error">{{ checkpoint.error }}</p><small>最近保存 {{ time(checkpoint.updated_at) }}</small></div>
            <div class="job-actions"><n-button v-if="checkpoint.draft_available" size="small" @click="downloadDraft(checkpoint.number,checkpoint.content_hash)">取回生成稿</n-button><router-link v-if="!checkpoint.stale" :to="`/book/${novel}/workbench?chapter=${checkpoint.number}`">返回创作处理 →</router-link></div>
          </article>
        </section>
        <section class="operations-section">
          <div class="section-heading"><div><h2>模型请求明细</h2><p>累计记录，每页 100 条。核对账单会更新预算计入值，保留原记录与每次调整依据。</p></div><n-checkbox v-model:checked="uncertainOnly" @update:checked="offset = 0; load()">仅看待核对</n-checkbox></div>
          <p v-if="!data.calls.length" class="table-empty">暂无模型请求记录。</p>
          <div v-else class="usage-table-scroll"><table class="usage-table call-details"><thead><tr><th>时间 / 用途</th><th>模型</th><th>状态</th><th>输入 / 输出 Token</th><th>费用估算 / 核对</th></tr></thead><tbody><tr v-for="call in data.calls" :key="call.id"><td>{{ time(call.created_at) }}<small>{{ call.operation || '创作请求' }}</small></td><td>{{ call.model }}<small>{{ kinds[call.kind] }}</small></td><td>{{ call.status === 'completed' ? '已返回' : call.status === 'failed' ? '请求失败' : '在途 / 中断' }}<small>{{ call.revision ? `已核对 · 第 ${call.revision} 版` : call.input_tokens === null || call.output_tokens === null ? '用量待核对' : '服务商返回用量' }}</small></td><td>{{ call.input_tokens ?? '未返回' }} / {{ call.output_tokens ?? '未返回' }}<small v-if="call.input_tokens === null || call.output_tokens === null">预算预留 {{ format(call.reserved_tokens) }}</small></td><td>{{ call.cost !== null ? `¥ ${call.cost.toFixed(6)}` : call.reserved_cost !== null ? `¥ ${call.reserved_cost.toFixed(6)}（预留）` : '费用未知' }}<small><n-button text size="small" @click="openReconciliation(call)">{{ call.revision ? '查看核对' : '核对账单' }}</n-button></small></td></tr></tbody></table></div>
          <div v-if="data.calls_page.total" class="ledger-pagination"><span>第 {{ data.calls_page.offset + 1 }}–{{ Math.min(data.calls_page.offset + 100,data.calls_page.total) }} 条，共 {{ data.calls_page.total }} 条</span><n-button size="small" :disabled="offset === 0" @click="offset = Math.max(0,offset-100); load()">上一页</n-button><n-button size="small" :disabled="offset + 100 >= data.calls_page.total" @click="offset += 100; load()">下一页</n-button></div>
        </section>
      </template>
      <n-skeleton v-else-if="!error" text :repeat="8" />
    </main>
    <n-modal v-model:show="aftermathOpen" preset="card" title="恢复章后处理" :style="{width:'min(560px, calc(100vw - 32px))'}" :mask-closable="false" :close-on-esc="false" :closable="!aftermathSaving">
      <template v-if="aftermathTask"><p class="budget-help">第 {{ aftermathTask.number }} 章 · 已完成的阶段及本地写入会复用。恢复可能继续尚未发起的模型请求，仍受作品预算限制；不会重新生成正文。</p><n-alert v-if="aftermathTask.unknown_requests" type="warning" title="先核对可能已计费的请求">有 {{ aftermathTask.unknown_requests }} 个请求未留存可恢复结果。请先核对服务商用量，再明确决定是否重新请求。</n-alert><n-alert v-if="aftermathError" type="error" class="operations-alert">{{ aftermathError }}</n-alert><n-form label-placement="top" style="margin-top:20px"><n-form-item label="处理说明"><n-input v-model:value="aftermathNote" type="textarea" :input-props="{'aria-label':'章后恢复处理说明'}" maxlength="1000" placeholder="记录已核对的请求、配置修复或处理依据，不要填写密钥" /></n-form-item><n-checkbox v-model:checked="aftermathRetryModels">{{ aftermathTask.unknown_requests ? '我已核对用量，确认重新请求未留存结果的步骤，可能再次计费' : '重新请求未写入数据阶段的模型结果，可能再次计费' }}</n-checkbox></n-form><p class="budget-help">默认使用已保存的模型结果。若已有本地写入，只补做剩余步骤；修改了正文的旧任务不会恢复。</p></template>
      <template #footer><div class="operations-actions" style="justify-content:flex-end"><n-button :disabled="aftermathSaving" @click="aftermathOpen = false">暂不恢复</n-button><n-button type="primary" :loading="aftermathSaving" :disabled="!!(aftermathTask?.unknown_requests && !aftermathRetryModels) || (aftermathRetryModels && !aftermathNote.trim())" @click="resumeAftermath">确认恢复章后处理</n-button></div></template>
    </n-modal>
    <n-drawer v-model:show="policyOpen" width="min(660px, 100vw)" placement="right" class="budget-drawer">
      <n-drawer-content title="作品预算" :body-content-style="{padding:'24px'}" closable>
        <n-alert type="info" :bordered="false">累计与每日上限同时生效，清空表示不限，填 0 表示禁止新的模型请求。修改预算不会清除历史用量。</n-alert>
        <n-form label-placement="top" class="budget-form">
          <n-form-item label="累计模型请求次数上限"><n-input-number v-model:value="draft.max_calls" :min="0" clearable placeholder="不限" /></n-form-item>
          <n-form-item label="累计 Token 上限"><n-input-number v-model:value="draft.max_tokens" :min="0" clearable placeholder="不限" /></n-form-item>
          <n-form-item label="累计费用估算上限（人民币）"><n-input-number v-model:value="draft.max_cost" :min="0" clearable placeholder="不限" /></n-form-item>
          <h3>每日限额</h3><p class="budget-help">按以下时区的零点重置。切换时区会按同一批请求的发起时间重新划分日期，累计用量不变。</p>
          <n-form-item label="每日统计时区（IANA）"><n-input v-model:value="draft.timezone" :input-props="{'aria-label':'每日统计时区'}" placeholder="Asia/Shanghai" /></n-form-item>
          <n-form-item label="每日模型请求次数上限"><n-input-number v-model:value="draft.daily_max_calls" :input-props="{'aria-label':'每日请求上限'}" :min="0" clearable placeholder="每日不限" /></n-form-item>
          <n-form-item label="每日 Token 上限"><n-input-number v-model:value="draft.daily_max_tokens" :input-props="{'aria-label':'每日 Token 上限'}" :min="0" clearable placeholder="每日不限" /></n-form-item>
          <n-form-item label="每日费用估算上限（人民币）"><n-input-number v-model:value="draft.daily_max_cost" :input-props="{'aria-label':'每日金额上限'}" :min="0" clearable placeholder="每日不限" /></n-form-item>
          <h3>模型单价</h3><p class="budget-help">按服务商当前价格填写，单位为元 / 百万 Token。单价为 0 时估算费用也为 0；金额预算需要所有请求模型均有单价。输入按未命中缓存价格填写。</p>
          <div v-for="(price,index) in draft.prices" :key="index" class="price-editor"><div class="price-model"><n-select v-model:value="price.kind" :options="kindOptions" aria-label="模型类型" /><n-input v-model:value="price.model" placeholder="与请求完全一致的模型名" aria-label="计费模型名" /><n-button quaternary @click="draft.prices.splice(index,1)" :aria-label="`删除第 ${index+1} 项单价`">删除</n-button></div><div class="price-values"><n-form-item label="输入单价"><n-input-number v-model:value="price.input_per_million" :min="0" :show-button="false" :aria-label="`第 ${index+1} 项输入单价`" /></n-form-item><n-form-item label="输出单价（嵌入 / 重排填 0）"><n-input-number v-model:value="price.output_per_million" :min="0" :show-button="false" :aria-label="`第 ${index+1} 项输出单价`" /></n-form-item></div></div>
          <n-button dashed block @click="draft.prices.push({kind:'llm',model:'',input_per_million:0,output_per_million:0})">添加模型单价</n-button>
        </n-form>
        <p class="budget-help">请求前按输入字节与输出上限预留，可能比最终用量保守。失败或未返回用量的请求不会自动释放预留，避免重复消费。服务商实际计费以其账单为准。</p>
        <template #footer><span v-if="dirty" class="unsaved-budget">有未保存修改</span><n-button type="primary" :loading="saving" :disabled="!dirty" @click="save">保存预算</n-button></template>
      </n-drawer-content>
    </n-drawer>
    <n-drawer :show="reconciliationOpen" width="min(660px, 100vw)" placement="right" :mask-closable="false" :close-on-esc="false">
      <n-drawer-content title="核对模型账单" :body-content-style="{padding:'24px'}">
        <template #header><div class="reconciliation-heading"><span>核对模型账单</span><n-button text :disabled="reconciling" @click="closeReconciliation">关闭核对</n-button></div></template>
        <template v-if="reconciliationCall">
          <div class="reconciliation-source"><strong>{{ reconciliationCall.model }}</strong><p>{{ kinds[reconciliationCall.kind] }} · {{ time(reconciliationCall.created_at) }}</p><code>{{ reconciliationCall.id }}</code><p>原始返回：{{ reconciliationCall.original_input_tokens ?? '未知' }} / {{ reconciliationCall.original_output_tokens ?? '未知' }} Token</p><p>原始预算预留：{{ format(reconciliationCall.reserved_tokens) }} Token</p><p>原始费用估算：{{ reconciliationCall.original_cost === null ? '未知' : `¥ ${reconciliationCall.original_cost.toFixed(6)}` }} · 当前计入金额：{{ reconciliationCall.cost === null ? '尚未结算' : `¥ ${reconciliationCall.cost.toFixed(6)}` }}</p></div>
          <p class="budget-help">请先在服务商确认请求结束，再填写对应请求的用量。核对只调整账本，不重新请求模型或恢复任务。不要填写 API Key。</p>
          <n-alert v-if="reconciliationError" type="error" class="operations-alert">{{ reconciliationError }}</n-alert>
          <n-form label-placement="top" class="reconcile-form">
            <div class="price-values"><n-form-item label="核实输入 Token"><n-input-number v-model:value="reconcileDraft.input_tokens" :min="0" :input-props="{'aria-label':'核实输入 Token'}" :show-button="false" /></n-form-item><n-form-item label="核实输出 Token"><n-input-number v-model:value="reconcileDraft.output_tokens" :min="0" :input-props="{'aria-label':'核实输出 Token'}" :show-button="false" /></n-form-item></div>
            <n-form-item label="服务商账单金额（人民币，可留空）"><n-input-number v-model:value="reconcileDraft.billed_cost" :min="0" :input-props="{'aria-label':'服务商账单金额'}" :show-button="false" placeholder="留空沿用此请求保存的单价" /></n-form-item>
            <n-form-item label="核对依据（必填）"><n-input v-model:value="reconcileDraft.note" type="textarea" :input-props="{'aria-label':'核对依据'}" maxlength="1000" placeholder="例如：服务商请求明细显示输入 120、输出 80 Token" /></n-form-item>
            <n-form-item label="账单或请求编号（选填）"><n-input v-model:value="reconcileDraft.reference" :input-props="{'aria-label':'账单或请求编号'}" maxlength="240" placeholder="填写便于再次核查的编号" /></n-form-item>
            <n-checkbox v-model:checked="reconcileDraft.request_finished">我已在服务商核实此请求结束及对应的用量</n-checkbox>
          </n-form>
          <div v-if="tokenDifference !== null" class="reconciliation-difference"><span>本次预算计入 Token 变化</span><strong>{{ signed(tokenDifference) }}</strong><p>保存后更新累计及请求发起日的用量。金额未填时沿用该请求的单价，无单价则保持费用未知。</p></div>
          <h3 class="ledger-history-title">核对历史</h3><n-skeleton v-if="reconciliationLoading" text :repeat="3" /><p v-else-if="!reconciliationHistory.length" class="budget-help">尚无核对记录，原始请求会完整保留。</p>
          <article v-for="entry in reconciliationHistory" :key="entry.id" class="reconciliation-entry"><header><strong>第 {{ entry.revision }} 版</strong><time>{{ time(entry.created_at) }}</time></header><p>{{ entry.before.input_tokens ?? '未知' }} / {{ entry.before.output_tokens ?? '未知' }} → {{ entry.after.input_tokens }} / {{ entry.after.output_tokens }} Token</p><p>预算计入差额 {{ signed(entry.difference.charged_tokens) }} Token · {{ entry.difference.cost === null ? '金额差额未知' : `金额差额 ¥ ${entry.difference.cost.toFixed(6)}` }}</p><p>核对后金额：{{ entry.after.cost === null ? '未提供金额或单价' : `¥ ${entry.after.cost.toFixed(6)}` }}</p><p>{{ entry.note }}</p><small v-if="entry.reference">编号：{{ entry.reference }}</small></article>
        </template>
        <template #footer><n-button :disabled="reconciling" @click="closeReconciliation">取消核对</n-button><n-button type="primary" :loading="reconciling" :disabled="!reconciliationValid || reconciliationLoading" @click="reconcile">保存账单核对</n-button></template>
      </n-drawer-content>
    </n-drawer>
  </div>
</template>

<style scoped>
.operations-shell{min-height:100vh;background:var(--app-bg,#f7f8fa);color:var(--app-text-primary,#202c38)}
.operations-desk{margin-left:216px;max-width:1600px;padding:30px 44px 64px}
.operations-crumb{display:flex;gap:12px;font-size:12px;color:var(--app-text-muted);margin-bottom:32px}.operations-crumb a{color:inherit;text-decoration:none}
.operations-header{display:flex;justify-content:space-between;align-items:center;gap:24px;margin-bottom:30px}.operations-kicker{font-size:11px;letter-spacing:.12em;color:var(--color-brand);margin:0 0 8px}.operations-header h1{font-size:30px;letter-spacing:.02em;margin:0 0 8px;font-weight:650}.operations-header p:last-child{color:var(--app-text-secondary);font-size:13px;margin:0}.operations-actions{display:flex;gap:10px}.operations-alert{margin-bottom:20px}
.usage-strip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border:1px solid var(--app-border);border-radius:10px;background:var(--app-surface);padding:24px 0}.usage-strip>div{padding:0 24px;border-right:1px solid var(--app-border)}.usage-strip>div:last-child{border:0}.usage-strip span{font-size:12px;color:var(--app-text-secondary)}.usage-strip strong{display:block;font-size:27px;font-weight:600;line-height:1.7;font-variant-numeric:tabular-nums}.usage-strip small{font-size:13px;font-weight:400}.usage-strip p{font-size:11px;margin:0;color:var(--app-text-muted);line-height:1.6}.usage-note{font-size:12px;color:var(--app-text-muted);line-height:1.9;margin:14px 0 30px;max-width:950px}
.operations-section{background:var(--app-surface);border:1px solid var(--app-border);border-radius:10px;margin-bottom:24px;overflow:hidden}.section-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:22px 24px;border-bottom:1px solid var(--app-border)}.section-heading h2{font-size:16px;margin:0 0 5px;font-weight:600}.section-heading p,.section-heading>span{font-size:12px;color:var(--app-text-muted);margin:0;line-height:1.8}
.job-row{display:flex;align-items:flex-start;gap:18px;padding:22px 24px;border-bottom:1px solid var(--app-border)}.job-row:last-child{border:0}.job-status{font-size:11px;padding:4px 8px;border-radius:4px;white-space:nowrap;background:var(--app-surface-subtle);color:var(--app-text-secondary);margin-top:2px}.job-status.running,.job-status.queued{color:#315b85;background:#edf4fc}.job-status.completed{color:#2a6b57;background:#edf6f2}.job-status.failed,.job-status.interrupted{color:#9b5933;background:#fbf0e8}.job-body{flex:1;min-width:0}.job-body h3{font-size:14px;margin:0 0 6px;font-weight:550}.job-body p{font-size:12px;margin:0 0 7px;line-height:1.8;color:var(--app-text-secondary);overflow-wrap:anywhere}.job-body .job-error{color:#9b5933}.job-body small{font-size:11px;color:var(--app-text-muted)}.job-actions{padding-top:2px}.job-actions a,.operations-empty a{font-size:12px;color:var(--color-brand);text-decoration:none}
.operations-empty{padding:40px 24px;text-align:center;color:var(--app-text-muted)}.operations-empty h3{font-size:15px;color:var(--app-text-primary);font-weight:500;margin:12px 0 6px}.operations-empty p{font-size:12px;margin:0 0 16px}.table-empty{padding:24px;font-size:13px;color:var(--app-text-muted)}.usage-table-scroll{overflow-x:auto}.usage-table{width:100%;border-collapse:collapse;font-size:12px;white-space:nowrap;text-align:left}.usage-table th{font-weight:500;color:var(--app-text-secondary);background:var(--app-surface-subtle);padding:12px 24px}.usage-table td{padding:16px 24px;border-top:1px solid var(--app-border);font-variant-numeric:tabular-nums}.usage-table td small{display:block;color:var(--app-text-muted);margin-top:5px;font-size:11px}
.budget-form{margin-top:24px}.budget-form :deep(.n-input-number){width:100%}.budget-help{font-size:12px;line-height:1.9;color:var(--app-text-secondary)}.price-editor{border-top:1px solid var(--app-border);padding-top:16px;margin-top:16px}.price-model{display:grid;grid-template-columns:115px 1fr auto;gap:8px}.price-values{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:14px}.unsaved-budget{font-size:12px;color:var(--app-text-muted);margin-right:20px}
.report-controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.report-controls label{font-size:12px;color:var(--app-text-secondary)}.report-controls input{margin-left:6px;font:inherit;color:var(--app-text-primary);background:var(--app-surface);border:1px solid var(--app-border);border-radius:4px;padding:6px}.report-controls input:focus-visible{outline:2px solid var(--color-brand);outline-offset:2px}
.daily-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));margin:0;padding:22px 24px;gap:20px}.daily-summary dt{font-size:12px;color:var(--app-text-secondary);margin-bottom:10px}.daily-summary dd{margin:0;font-size:21px;font-weight:550;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.daily-summary small{font-size:12px;color:var(--app-text-muted);font-weight:400}.daily-note{margin:0;padding:0 24px 20px;font-size:12px;color:var(--app-text-muted);line-height:1.8}.ledger-pagination{display:flex;align-items:center;justify-content:flex-end;gap:10px;padding:14px 24px;border-top:1px solid var(--app-border);font-size:12px;color:var(--app-text-muted);flex-wrap:wrap}
.reconciliation-heading{display:flex;align-items:center;justify-content:space-between;gap:20px;width:100%}.reconciliation-source{border-left:3px solid var(--color-brand);padding:6px 0 6px 16px;background:var(--app-surface-subtle)}.reconciliation-source strong{font-size:16px}.reconciliation-source p,.reconciliation-source code{font-size:12px;color:var(--app-text-secondary);overflow-wrap:anywhere}.reconcile-form :deep(.n-input-number){width:100%}.reconciliation-difference{border:1px solid var(--app-border);border-radius:6px;padding:16px;margin:20px 0}.reconciliation-difference>span{font-size:12px;color:var(--app-text-secondary)}.reconciliation-difference strong{display:block;font-size:24px;font-variant-numeric:tabular-nums;margin:8px 0}.reconciliation-difference p,.reconciliation-entry p{font-size:12px;line-height:1.8;color:var(--app-text-secondary);margin:4px 0;overflow-wrap:anywhere}.ledger-history-title{font-size:14px;margin:24px 0 12px}.reconciliation-entry{border-top:1px solid var(--app-border);padding:16px 0}.reconciliation-entry header{display:flex;justify-content:space-between;gap:12px;font-size:12px;margin-bottom:8px}.reconciliation-entry time,.reconciliation-entry small{font-size:11px;color:var(--app-text-muted);overflow-wrap:anywhere}
@media(max-width:760px){.daily-ledger .section-heading{flex-direction:column;align-items:flex-start}.daily-summary{grid-template-columns:1fr;padding:20px 16px;gap:16px}.daily-summary>div{display:flex;justify-content:space-between;gap:12px}.daily-summary dt{margin:5px 0;max-width:45%}.daily-summary dd{font-size:18px;text-align:right}.daily-note{padding:0 16px 16px}.price-values{grid-template-columns:1fr;gap:0}.reconciliation-heading{font-size:16px}}
@media(max-width:1150px){.operations-desk{padding:26px}.usage-strip{grid-template-columns:repeat(2,minmax(0,1fr));gap:24px 0}.usage-strip>div:nth-child(2){border:0}.operations-header{align-items:flex-start;flex-direction:column}}
@media(max-width:760px){.operations-desk{margin-left:0;padding:68px 16px 32px}.operations-crumb{margin-bottom:24px}.operations-header h1{font-size:26px}.usage-strip>div{padding:0 16px}.usage-strip strong{font-size:22px}.job-row{padding:18px 16px;gap:10px;flex-wrap:wrap}.job-body{min-width:180px}.job-actions{margin-left:auto}.section-heading{padding:18px 16px}.operations-actions{flex-wrap:wrap}.price-model{grid-template-columns:95px 1fr}.price-model>.n-button{grid-column:2;justify-self:end}}
</style>
