<script setup lang="ts">
import { computed, ref } from 'vue'
import { evidenceReviewApi, type RetrievalConfig } from '@/api/evidenceReview'
import AppIcon from '@/components/layout/AppIcon.vue'
import { formatApiError } from '@/utils/apiError'
import { useMessage } from 'naive-ui'
const message = useMessage()
const show = ref(false), loading = ref(false), saving = ref(false), testing = ref(false)
const config = ref<RetrievalConfig | null>(null)
const apiKey = ref(''), error = ref(''), verified = ref('')
const form = ref({base_url: 'https://api.siliconflow.cn/v1', embedding_model: 'Qwen/Qwen3-Embedding-4B', reranker_model: 'Qwen/Qwen3-Reranker-4B', dimensions: 1024})
const dirty = computed(() => !!apiKey.value || !config.value || Object.entries(form.value).some(([key,value]) => config.value?.[key as keyof RetrievalConfig] !== value))
const embeddingModels = ['Qwen/Qwen3-Embedding-4B','Qwen/Qwen3-Embedding-8B','Qwen/Qwen3-Embedding-0.6B','BAAI/bge-m3'].map(value=>({value,label:value}))
const rerankerModels = ['Qwen/Qwen3-Reranker-4B','Qwen/Qwen3-Reranker-8B','Qwen/Qwen3-Reranker-0.6B','BAAI/bge-reranker-v2-m3'].map(value=>({value,label:value}))
async function open() {
 show.value = true; loading.value = true; error.value = ''; apiKey.value = ''; verified.value = ''
 try { const value = await evidenceReviewApi.retrievalConfig(); config.value = value; form.value = {base_url:value.base_url,embedding_model:value.embedding_model,reranker_model:value.reranker_model,dimensions:value.dimensions} }
 catch(e) { error.value = formatApiError(e, '读取检索配置失败') }
 finally { loading.value = false }
}
async function save() {
 saving.value = true; error.value = ''; verified.value = ''
 try { config.value = await evidenceReviewApi.saveRetrievalConfig({...form.value, ...(apiKey.value.trim() ? {api_key:apiKey.value.trim()} : {})}); apiKey.value = ''; message.success('检索配置已保存') }
 catch(e) { error.value = formatApiError(e, '保存检索配置失败') }
 finally { saving.value = false }
}
async function test() {
 testing.value = true; error.value = ''; verified.value = ''
 try { const value = await evidenceReviewApi.testRetrieval(); verified.value = `Embedding 与 Reranker 均连接成功，实际向量维度 ${value.dimension}。` }
 catch(e) { error.value = formatApiError(e, '检索模型测试失败') }
 finally { testing.value = false }
}
</script>

<template>
 <button type="button" class="retrieval-settings-entry" @click="open"><AppIcon name="search" :size="17"/>检索模型</button>
 <n-modal v-model:show="show" preset="card" title="检索模型配置" :style="{width:'min(560px,94vw)'}" :mask-closable="!saving && !testing">
  <n-spin :show="loading">
   <p class="retrieval-intro">用于检索历史章节和排序证据。与 DeepSeek 聊天档案分开，共用本地嵌入模型配置。</p>
   <n-alert v-if="error" type="error" :show-icon="false" class="retrieval-notice">{{error}}</n-alert>
   <n-alert v-if="verified" type="success" :show-icon="false" class="retrieval-notice">{{verified}}</n-alert>
   <n-form label-placement="top">
    <n-form-item label="Base URL"><n-input v-model:value="form.base_url" :disabled="saving || testing" :input-props="{'aria-label':'检索接口地址'}"/></n-form-item>
    <n-form-item label="API Key"><n-input v-model:value="apiKey" type="password" show-password-on="click" :placeholder="config?.api_key_configured ? '已保存；留空保留现有 Key' : '只在本地填写'" :disabled="saving || testing" :input-props="{'aria-label':'检索 API Key'}"/></n-form-item>
    <n-form-item label="Embedding 模型"><n-select v-model:value="form.embedding_model" filterable tag :options="embeddingModels" :disabled="saving || testing"/></n-form-item>
    <n-form-item label="Reranker 模型"><n-select v-model:value="form.reranker_model" filterable tag :options="rerankerModels" :disabled="saving || testing"/></n-form-item>
    <n-form-item label="向量维度（Qwen3）"><n-select v-model:value="form.dimensions" :options="[256,512,768,1024,2048].map(value=>({value,label:String(value)}))" :disabled="saving || testing"/></n-form-item>
   </n-form>
   <p class="retrieval-intro">连接测试只发送 3 条短文本及 1 次重排。首次使用混合检索会为前置章节创建索引，后续复用缓存；服务商按实际请求计费。</p>
  </n-spin>
  <template #footer><n-space justify="end"><n-button :loading="testing" :disabled="loading || saving || dirty || !config?.configured" @click="test">测试两个模型</n-button><n-button type="primary" :loading="saving" :disabled="loading || testing" @click="save">保存检索配置</n-button></n-space></template>
 </n-modal>
</template>

<style scoped>
.retrieval-settings-entry{display:flex;align-items:center;gap:11px;width:100%;padding:11px 12px;border:0;background:none;border-radius:7px;color:var(--app-text-secondary);font:500 13px var(--font-sans);cursor:pointer;text-align:left}
.retrieval-settings-entry:hover{background:var(--app-surface-subtle)}
.retrieval-settings-entry:focus-visible{outline:2px solid var(--color-brand);outline-offset:2px}
.retrieval-intro{font-size:12px;color:var(--app-text-muted);line-height:1.9;margin-bottom:20px}
.retrieval-notice{margin-bottom:18px}
</style>
