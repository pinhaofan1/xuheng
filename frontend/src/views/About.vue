<script setup lang="ts">
import AboutBrandSection from '@/components/settings/sections/AboutBrandSection.vue'
</script>
<template><main class="about-page"><router-link to="/">← 返回书目</router-link><AboutBrandSection /></main></template>
<style scoped>.about-page { max-width: 860px; margin: 40px auto; padding: 24px; } .about-page > a { display: inline-block; margin-bottom: 24px; color: var(--app-text-secondary); }</style>
