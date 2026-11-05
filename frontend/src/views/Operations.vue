<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, onBeforeRouteLeave } from 'vue-router'
import { useDialog, useMessage } from 'naive-ui'
import WorkspaceNav from '@/components/layout/WorkspaceNav.vue'
import AppIcon from '@/components/layout/AppIcon.vue'
import { operationsApi, type Operations, type UsagePolicy } from '@/api/operations'
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
const draft = ref<UsagePolicy>({ max_calls: null, max_tokens: null, max_cost: null, prices: [] })
const saved = ref('')
const dirty = computed(() => saved.value !== '' && JSON.stringify(draft.value) !== saved.value)
const policyOpen = ref(false)
const clock = ref(Date.now())
let timer: ReturnType<typeof setTimeout> | undefined
let sequence = 0
let disposed = false
const statuses: Record<string,string> = { queued: '排队中', running: '进行中', completed: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断' }
const kinds: Record<string,string> = { llm: '大模型', embedding: 'Embedding', rerank: 'Reranker' }
const kindOptions = Object.entries(kinds).map(([value,label]) => ({value,label}))
const format = (n: number) => n.toLocaleString('zh-CN')
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
    const result = await operationsApi.get(novel.value)
    if (id !== sequence || disposed) return
    data.value = result; error.value = ''; clock.value = Date.now()
    if (initial) { draft.value = structuredClone(result.policy); saved.value = JSON.stringify(result.policy) }
  } catch (e) { if (id === sequence && !disposed) error.value = formatApiError(e, '读取任务记录失败，请重试') }
  finally { if (id === sequence && !disposed) timer = setTimeout(() => void load(), 5000) }
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
  if (!dirty.value) return true
  return new Promise<boolean>(resolve => dialog.warning({ title: '预算尚未保存', content: '离开将放弃本次预算修改。', positiveText: '放弃修改', negativeText: '继续编辑', onPositiveClick: () => resolve(true), onNegativeClick: () => resolve(false), onClose: () => resolve(false), onMaskClick: () => resolve(false) }))
})
watch(novel, () => { data.value = null; saved.value = ''; void load(true) }, { immediate: true })
onBeforeUnmount(() => { disposed = true; ++sequence; clearTimeout(timer) })
</script>

<template>
  <div class="operations-shell">
    <WorkspaceNav section="operations" :novel-id="novel" />
    <main class="operations-desk">
      <div class="operations-crumb"><router-link :to="`/book/${novel}/workbench`">章节创作</router-link><span>/</span><span>任务与用量</span></div>
      <header class="operations-header"><div><p class="operations-kicker">创作记录</p><h1>任务与用量</h1><p>查看执行进度，保留每一次请求的记录。</p></div><div class="operations-actions"><n-button @click="load()"><template #icon><AppIcon name="history" /></template>刷新记录</n-button><n-button type="primary" :disabled="!data" @click="policyOpen = true">设置作品预算</n-button></div></header>
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
        <section class="operations-section">
          <div class="section-heading"><div><h2>审校任务</h2><p>每 5 秒更新。重新发起需要确认，不会自动重复请求。</p></div><span>{{ data.jobs.length }} 条记录</span></div>
          <div v-if="!data.jobs.length" class="operations-empty"><AppIcon name="review" :size="28" /><h3>还没有审校任务</h3><p>在章节中发起 AI 深度审校后，可在这里查看进度和结果。</p><router-link :to="`/book/${novel}/workbench`">返回章节创作 →</router-link></div>
          <article v-for="job in data.jobs" :key="job.id" class="job-row">
            <span class="job-status" :class="job.status">{{ statuses[job.status] }}</span>
            <div class="job-body"><h3>第 {{ job.number }} 章 · AI 一致性审校</h3><p>{{ job.stage }}</p><p v-if="job.error" class="job-error">{{ job.error }}</p><small>{{ time(job.created_at) }}<template v-if="active(job)"> · 执行心跳 {{ age(job) }}<span v-if="clock - Date.parse(job.updated_at) > 20000">，执行进程可能失去响应</span></template><template v-if="job.retry_of"> · 重新发起</template></small></div>
            <div class="job-actions"><router-link v-if="job.status === 'completed'" :to="`/book/${novel}/review/${job.number}?mode=ai&run=${job.run_id}`">查看结果 →</router-link><n-button v-else-if="active(job)" size="small" :disabled="job.cancel_requested || pending === job.id" @click="cancel(job)">请求取消</n-button><n-button v-else size="small" :disabled="!!pending" @click="retry(job)">重新发起</n-button></div>
          </article>
        </section>
        <section class="operations-section">
          <div class="section-heading"><div><h2>模型请求明细</h2><p>覆盖已接入的大模型、Embedding 与 Reranker 请求，显示最近 100 条。</p></div></div>
          <p v-if="!data.calls.length" class="table-empty">暂无模型请求记录。</p>
          <div v-else class="usage-table-scroll"><table class="usage-table"><thead><tr><th>时间 / 用途</th><th>模型</th><th>状态</th><th>输入 / 输出 Token</th><th>费用估算</th></tr></thead><tbody><tr v-for="call in data.calls" :key="call.id"><td>{{ time(call.created_at) }}<small>{{ call.operation || '创作请求' }}</small></td><td>{{ call.model }}<small>{{ kinds[call.kind] }}</small></td><td>{{ call.status === 'completed' ? '已返回' : call.status === 'failed' ? '失败，待核对用量' : '在途 / 中断待核对' }}</td><td>{{ call.input_tokens ?? '未返回' }} / {{ call.output_tokens ?? '未返回' }}<small v-if="call.input_tokens === null || call.output_tokens === null">预算预留 {{ format(call.reserved_tokens) }}</small></td><td>{{ call.cost !== null ? `¥ ${call.cost.toFixed(6)}` : call.reserved_cost !== null ? `¥ ${call.reserved_cost.toFixed(6)}（预留）` : '未配置单价' }}</td></tr></tbody></table></div>
        </section>
      </template>
      <n-skeleton v-else-if="!error" text :repeat="8" />
    </main>
    <n-drawer v-model:show="policyOpen" width="min(660px, 100vw)" placement="right" class="budget-drawer">
      <n-drawer-content title="作品预算" :body-content-style="{padding:'24px'}" closable>
        <n-alert type="info" :bordered="false">上限按作品累计，清空表示不限，填 0 表示禁止新的模型请求。修改预算不会清除历史用量。</n-alert>
        <n-form label-placement="top" class="budget-form">
          <n-form-item label="累计模型请求次数上限"><n-input-number v-model:value="draft.max_calls" :min="0" clearable placeholder="不限" /></n-form-item>
          <n-form-item label="累计 Token 上限"><n-input-number v-model:value="draft.max_tokens" :min="0" clearable placeholder="不限" /></n-form-item>
          <n-form-item label="累计费用估算上限（人民币）"><n-input-number v-model:value="draft.max_cost" :min="0" clearable placeholder="不限" /></n-form-item>
          <h3>模型单价</h3><p class="budget-help">按服务商当前价格填写，单位为元 / 百万 Token。单价为 0 时估算费用也为 0；金额预算需要所有请求模型均有单价。输入按未命中缓存价格填写。</p>
          <div v-for="(price,index) in draft.prices" :key="index" class="price-editor"><div class="price-model"><n-select v-model:value="price.kind" :options="kindOptions" aria-label="模型类型" /><n-input v-model:value="price.model" placeholder="与请求完全一致的模型名" aria-label="计费模型名" /><n-button quaternary @click="draft.prices.splice(index,1)" :aria-label="`删除第 ${index+1} 项单价`">删除</n-button></div><div class="price-values"><n-form-item label="输入单价"><n-input-number v-model:value="price.input_per_million" :min="0" :show-button="false" :aria-label="`第 ${index+1} 项输入单价`" /></n-form-item><n-form-item label="输出单价（嵌入 / 重排填 0）"><n-input-number v-model:value="price.output_per_million" :min="0" :show-button="false" :aria-label="`第 ${index+1} 项输出单价`" /></n-form-item></div></div>
          <n-button dashed block @click="draft.prices.push({kind:'llm',model:'',input_per_million:0,output_per_million:0})">添加模型单价</n-button>
        </n-form>
        <p class="budget-help">请求前按输入字节与输出上限预留，可能比最终用量保守。失败或未返回用量的请求不会自动释放预留，避免重复消费。服务商实际计费以其账单为准。</p>
        <template #footer><span v-if="dirty" class="unsaved-budget">有未保存修改</span><n-button type="primary" :loading="saving" :disabled="!dirty" @click="save">保存预算</n-button></template>
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
@media(max-width:1150px){.operations-desk{padding:26px}.usage-strip{grid-template-columns:repeat(2,minmax(0,1fr));gap:24px 0}.usage-strip>div:nth-child(2){border:0}.operations-header{align-items:flex-start;flex-direction:column}}
@media(max-width:760px){.operations-desk{margin-left:0;padding:68px 16px 32px}.operations-crumb{margin-bottom:24px}.operations-header h1{font-size:26px}.usage-strip>div{padding:0 16px}.usage-strip strong{font-size:22px}.job-row{padding:18px 16px;gap:10px;flex-wrap:wrap}.job-body{min-width:180px}.job-actions{margin-left:auto}.section-heading{padding:18px 16px}.operations-actions{flex-wrap:wrap}.price-model{grid-template-columns:95px 1fr}.price-model>.n-button{grid-column:2;justify-self:end}}
</style>
