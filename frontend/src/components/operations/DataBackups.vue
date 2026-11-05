<script setup lang="ts">
import { ref, watch } from 'vue'
import { apiClient, resolveHttpUrl } from '@/api/config'
import { formatApiError } from '@/utils/apiError'
interface Backup {id:string;created_at:string;release:string;bytes?:number;valid_manifest?:boolean;counts:{main:{novels:number;chapters:number};operations:{calls:number}}}
const open = defineModel<boolean>('show',{default:false})
const rows = ref<Backup[]>([])
const pending = ref(false)
const error = ref('')
const preview = ref<Backup | null>(null)
const fileInput = ref<HTMLInputElement>()
const root = '/maintenance/backups'
const date = (value:string) => new Date(value).toLocaleString('zh-CN',{hour12:false})
async function run(action:()=>Promise<void>) {
  if (pending.value) return
  pending.value=true;error.value=''
  try { await action() } catch(e) {error.value=formatApiError(e,'备份操作未完成，请重试')}
  finally {pending.value=false}
}
async function refresh() {rows.value=await apiClient.get<Backup[]>(root)}
function create() {void run(async()=>{preview.value=null;preview.value=await apiClient.post<Backup>(root,{}, {timeout:60000});await refresh()})}
function verify(id:string) {void run(async()=>{preview.value=null;preview.value=await apiClient.post<Backup>(root+'/'+id+'/verify',{}, {timeout:60000})})}
function inspect(event:Event) {
  const file=(event.target as HTMLInputElement).files?.[0]
  if (!file) return
  preview.value=null
  void run(async()=>{
    const form=new FormData();form.append('file',file)
    preview.value=await apiClient.post<Backup>(root+'/inspect',form,{timeout:60000,headers:{'Content-Type':'multipart/form-data'}})
  })
  ;(event.target as HTMLInputElement).value=''
}
watch(open,value=>{if(value){preview.value=null;void run(refresh)}})
</script>
<template>
  <n-drawer v-model:show="open" width="min(720px, 100vw)" :auto-focus="false" :mask-closable="!pending" :close-on-esc="!pending">
    <n-drawer-content title="数据备份与恢复" :body-content-style="{padding:'20px',overflowWrap:'anywhere'}" :closable="!pending">
      <p class="backup-intro">联合保存所有作品、章节、设定、处理记录和模型用量账本，恢复时保持两份数据库对应。</p>
      <n-alert type="warning" :show-icon="false">备份含小说正文与数据库内的模型配置、密钥，请仅保存到可信位置；环境变量、独立向量索引和日志不在备份中。</n-alert>
      <div class="backup-actions"><n-button type="primary" :loading="pending" :disabled="pending" @click="create">创建联合备份</n-button><n-button :disabled="pending" @click="fileInput?.click()">校验本地备份</n-button><input ref="fileInput" type="file" accept=".zip" aria-label="选择备份文件" hidden @change="inspect" /></div>
      <n-alert v-if="error" type="error" title="操作未完成">{{ error }}</n-alert>
      <section v-if="preview" class="backup-preview" aria-label="备份校验结果"><strong>校验通过</strong><p>{{ date(preview.created_at) }} · 版本 {{ preview.release }}</p><p>{{ preview.counts.main.novels }} 部作品 · {{ preview.counts.main.chapters }} 章 · {{ preview.counts.operations.calls }} 条模型请求</p><small>文件清单、SHA-256、版本与数据库完整性均通过检查。</small></section>
      <h3>本机备份</h3><p v-if="!rows.length && !pending" class="backup-intro">尚无备份。创建后可下载到另一块磁盘保存。</p>
      <article v-for="item in rows" :key="item.id" class="backup-row"><div><strong>{{ item.valid_manifest ? date(item.created_at) : '备份信息异常' }}</strong><p v-if="item.valid_manifest">{{ item.counts.main.novels }} 部作品 · {{ item.counts.main.chapters }} 章 · {{ ((item.bytes || 0)/1048576).toFixed(1) }} MB</p><small>版本 {{ item.release || '未知' }}</small></div><div class="backup-row-actions"><n-button size="small" :disabled="pending" @click="verify(item.id)">校验</n-button><a :href="resolveHttpUrl('/api/v1'+root+'/'+item.id+'/export')" download>下载</a></div></article>
      <section class="backup-restore"><h3>恢复备份</h3><ol><li>先下载需要恢复的备份，并在此校验。</li><li>关闭叙衡服务及托管守护进程。</li><li>双击应用目录的 <code>restore-xuheng.cmd</code>，选择备份，核对预览后输入 RESTORE。</li><li>重新启动应用，检查正文与账目后手动继续创作。</li></ol><p>恢复前自动创建当前数据的安全副本；中断恢复会在下次脚本启动时回滚。恢复覆盖所有作品，完成后托管与未完成任务保持停止。</p></section>
    </n-drawer-content>
  </n-drawer>
</template>
<style scoped>
.backup-intro,.backup-restore p{color:var(--app-text-secondary);line-height:1.8;font-size:13px}.backup-actions{display:flex;flex-wrap:wrap;gap:12px;margin:24px 0}.backup-preview{padding:20px;background:var(--app-surface-subtle);border-left:3px solid var(--color-brand);margin:20px 0}.backup-preview>strong{color:var(--color-brand)}.backup-preview p{margin:8px 0}.backup-preview small,.backup-row small{color:var(--app-text-muted)}h3{font-size:15px;margin:28px 0 16px}.backup-row{padding:18px 0;border-top:1px solid var(--app-border);display:flex;justify-content:space-between;align-items:center;gap:16px}.backup-row p{margin:6px 0;font-size:12px}.backup-row-actions{display:flex;gap:16px;align-items:center;flex-shrink:0}.backup-row a{color:var(--color-brand)}.backup-restore{border-top:1px solid var(--app-border);margin-top:24px}.backup-restore ol{padding-left:22px;line-height:2;font-size:13px}.backup-restore code{overflow-wrap:anywhere}@media(max-width:500px){.backup-row{flex-wrap:wrap}.backup-row-actions{margin-left:auto}}
</style>
