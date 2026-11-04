<script setup lang="ts">
defineProps<{ error?: Error }>()

function refreshPage() {
  window.location.reload()
}
</script>

<template>
  <n-alert type="warning" title="此面板暂时无法加载" class="panel-load-error" role="alert">
    <p>页面资源可能已经更新或连接中断。请先保存正在编辑的正文，再刷新页面重试。</p>
    <n-button size="small" secondary @click="refreshPage">刷新页面</n-button>
  </n-alert>
</template>

<style scoped>
.panel-load-error { margin: 12px; }
.panel-load-error p { margin: 0 0 12px; line-height: 1.7; }
</style>
