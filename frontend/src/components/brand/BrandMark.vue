<template>
  <!-- Modified for Xuheng: original balance/page mark, drawn as SVG. -->
  <svg :width="size" :height="size" viewBox="0 0 64 64" role="img" aria-label="叙衡" class="brand-mark">
    <rect x="3" y="3" width="58" height="58" rx="16" fill="#214b58" />
    <path d="M32 14v35M18 23l28-5M18 23l-7 15h14l-7-15Zm28-5-7 15h14l-7-15ZM23 49h18" fill="none" stroke="#d8ece6" stroke-width="2.6" stroke-linejoin="round" stroke-linecap="round" />
    <circle cx="32" cy="14" r="3" fill="#d8b85e" />
  </svg>
</template>

<script setup lang="ts">
withDefaults(defineProps<{ size?: number }>(), { size: 48 })
</script>

<style scoped>
.brand-mark {
  display: block;
  flex-shrink: 0;
  /* 深色背景上给纸飞机一道细白边，防止深蓝端隐没 */
  filter: drop-shadow(0 0 1px rgba(255, 255, 255, 0.28));
}
</style>
