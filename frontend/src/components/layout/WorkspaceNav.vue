<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppIcon from './AppIcon.vue'
import BrandMark from '@/components/brand/BrandMark.vue'
import GlobalLLMEntryButton from '@/components/global/GlobalLLMEntryButton.vue'
import PromptPlazaEntryButton from '@/components/global/PromptPlazaEntryButton.vue'
import OnlineSquareEntryButton from '@/components/global/OnlineSquareEntryButton.vue'
import RetrievalSettings from '@/components/global/RetrievalSettings.vue'
import { chapterApi, type ChapterDTO } from '@/api/chapter'
import { novelApi } from '@/api/novel'
import { useAppSettingsShellStore } from '@/stores/appSettingsShellStore'

const props = defineProps<{ section: 'library' | 'review' | 'operations'; novelId?: string; chapterNumber?: number }>()
defineEmits<{ create: []; refresh: [] }>()
const route = useRoute()
const settings = useAppSettingsShellStore()
const chapters = ref<ChapterDTO[]>([])
const bookTitle = ref('')
const error = ref(false)
const open = ref(false)
const workbench = computed(() => `/book/${props.novelId}/workbench`)
let sequence = 0
async function loadBook() {
 const token = ++sequence
 chapters.value = []; bookTitle.value = ''; error.value = false
 if (!props.novelId) return
 try {
  const [novel, items] = await Promise.all([novelApi.getNovel(props.novelId), chapterApi.listChapters(props.novelId)])
  if (token !== sequence) return
  bookTitle.value = novel.title; chapters.value = [...items].sort((a,b) => a.number-b.number)
 } catch { if (token === sequence) error.value = true }
}
watch(() => props.novelId, loadBook, { immediate: true })
watch(() => route.fullPath, () => { open.value = false })
</script>

<template>
  <button type="button" class="xh-mobile-menu" aria-label="展开工作区导航" :aria-expanded="open" @click="open = !open"><AppIcon :name="open ? 'close' : 'menu'" /></button>
  <div v-if="open" class="xh-nav-scrim" @click="open = false" />
  <aside class="xh-nav" :class="{ 'is-open': open }" aria-label="工作区导航">
    <router-link to="/" class="xh-nav-brand"><BrandMark :size="32" /><span><strong>叙衡</strong><small>故事有序，创作有据</small></span></router-link>
    <div class="xh-nav-main">
      <p class="xh-nav-label">工作空间</p>
      <router-link to="/" class="xh-nav-item" :class="{ active: section === 'library' }" :aria-current="section === 'library' ? 'page' : undefined"><AppIcon name="library" />作品库</router-link>
      <button v-if="section === 'library'" type="button" class="xh-nav-item" @click="$emit('create')"><AppIcon name="plus" />新建作品</button>
      <template v-if="novelId">
        <p class="xh-nav-label xh-project-title" :title="bookTitle">{{ bookTitle || '当前作品' }}</p>
        <router-link :to="`${workbench}?chapter=${chapterNumber || 1}`" class="xh-nav-item"><AppIcon name="write" />章节创作</router-link>
        <router-link :to="`/book/${novelId}/review/${chapterNumber || 1}`" class="xh-nav-item" :class="{active: section === 'review'}" :aria-current="section === 'review' ? 'page' : undefined"><AppIcon name="review" />一致性审校</router-link>
        <router-link :to="`/book/${novelId}/operations`" class="xh-nav-item" :class="{active: section === 'operations'}" :aria-current="section === 'operations' ? 'page' : undefined"><AppIcon name="history" />任务与用量</router-link>
        <router-link :to="`${workbench}?panel=bible`" class="xh-nav-item"><AppIcon name="people" />人物与设定</router-link>
        <router-link :to="`${workbench}?panel=worldbuilding`" class="xh-nav-item"><AppIcon name="globe" />世界观与知识</router-link>
        <p class="xh-nav-label xh-chapter-label">章节目录 <span>{{ chapters.length }}</span></p>
        <p v-if="error" class="xh-nav-error">目录加载失败 <button @click="loadBook">重试</button></p>
        <nav class="xh-chapter-nav" aria-label="审校章节目录">
          <router-link v-for="chapter in chapters" :key="chapter.id" :to="{path: `/book/${novelId}/review/${chapter.number}`, query: {mode: route.query.mode}}" :class="{ current: chapter.number === chapterNumber }" :aria-current="chapter.number === chapterNumber ? 'page' : undefined"><span class="xh-chapter-number">{{ String(chapter.number).padStart(2, '0') }}</span><span>{{ chapter.title || `第 ${chapter.number} 章` }}</span></router-link>
        </nav>
      </template>
      <div class="xh-nav-tools"><p class="xh-nav-label">创作工具</p><GlobalLLMEntryButton appearance="sidebar" /><RetrievalSettings/><PromptPlazaEntryButton appearance="sidebar" /><OnlineSquareEntryButton /></div>
    </div>
    <footer class="xh-nav-footer"><button type="button" class="xh-nav-item" @click="settings.open()"><AppIcon name="settings" />应用设置</button><router-link to="/about">关于叙衡与许可 <AppIcon name="chevron" :size="14" /></router-link></footer>
  </aside>
</template>

<style scoped>
.xh-nav{position:fixed;
  inset:0 auto 0 0;
  width:216px;
  background:var(--app-surface);
  border-right:1px solid var(--app-border);
  display:flex;
  flex-direction:column;
  z-index:30;
  color:var(--app-text-secondary);
  font-size:13px}

.xh-nav-brand{height:92px;
  display:flex;
  align-items:center;
  gap:11px;
  padding:22px;
  text-decoration:none;
  color:var(--app-text-primary);
  flex-shrink:0}
.xh-nav-brand strong{display:block;
  font-size:22px;
  letter-spacing:3px;
  font-family:var(--xh-font-manuscript);
  font-weight:700}
.xh-nav-brand small{display:block;
  font-size:10px;
  color:var(--app-text-muted);
  margin-top:2px;
  letter-spacing:.04em}
.xh-nav-main{padding:8px 14px;
  flex:1;
  min-height:0;
  overflow:auto}
.xh-nav-label{display:flex;
  justify-content:space-between;
  padding:12px 12px 9px;
  font-size:10px;
  font-weight:600;
  letter-spacing:.06em;
  color:var(--app-text-muted)}
.xh-nav-item{width:100%;
  display:flex;
  align-items:center;
  gap:11px;
  padding:11px 12px;
  border:0;
  border-radius:7px;
  background:none;
  text-decoration:none;
  color:inherit;
  font:inherit;
  cursor:pointer;
  text-align:left;
  margin:2px 0}
.xh-nav-item:hover{background:var(--app-surface-subtle);
  color:var(--app-text-primary)}
.xh-nav-item.active{color:var(--color-brand);
  background:var(--color-brand-light);
  box-shadow:inset 3px 0 var(--color-brand);
  font-weight:600}
.xh-project-title{margin-top:18px;
  white-space:nowrap;
  overflow:hidden;
  text-overflow:ellipsis;
  display:block}
.xh-chapter-nav{margin-bottom:18px}
.xh-chapter-nav a{display:flex;
  gap:10px;
  font-size:12px;
  color:inherit;
  text-decoration:none;
  padding:9px 12px;
  border-radius:6px;
  line-height:1.7}
.xh-chapter-nav a span:last-child{overflow:hidden;
  text-overflow:ellipsis;
  white-space:nowrap}
.xh-chapter-number{font-variant-numeric:tabular-nums;
  color:var(--app-text-muted);
  font-size:11px}
.xh-chapter-nav a.current{color:var(--color-brand);
  background:var(--color-brand-light)}
.xh-nav-tools{margin-top:18px;
  display:grid;
  gap:5px}
.xh-nav-tools :deep(.variant-sidebar){background:transparent;
  border-color:transparent;
  border-radius:7px;
  box-shadow:none;
  font-weight:500}
.xh-nav-tools :deep(.variant-sidebar:hover){background:var(--app-surface-subtle)}
.xh-nav-footer{border-top:1px solid var(--app-border);
  padding:10px 14px 18px}
.xh-nav-footer>a{display:flex;
  justify-content:space-between;
  align-items:center;
  font-size:10px;
  color:var(--app-text-muted);
  text-decoration:none;
  padding:10px 12px 0}
.xh-nav-error{font-size:11px;
  padding:10px}
.xh-nav-error button{border:0;
  background:none;
  color:var(--color-brand);
  cursor:pointer}
.xh-mobile-menu{display:none}
.xh-nav-scrim{position:fixed;
  inset:0;
  background:#18223944;
  z-index:29}
.xh-nav a:focus-visible,.xh-nav button:focus-visible,.xh-mobile-menu:focus-visible{outline:2px solid var(--color-brand);
  outline-offset:2px}

@media(max-width:800px){.xh-nav{transform:translateX(-100%);
  transition:transform .2s;
  width:228px}
.xh-nav.is-open{transform:translateX(0)}
.xh-mobile-menu{display:grid;
  place-items:center;
  position:fixed;
  top:16px;
  left:16px;
  z-index:31;
  width:34px;
  height:34px;
  border:1px solid var(--app-border);
  border-radius:7px;
  background:var(--app-surface);
  color:var(--app-text-primary)}
.xh-nav.is-open .xh-nav-brand{padding-top:64px;
  height:130px}
}

@media(prefers-reduced-motion:reduce){.xh-nav{transition:none}
}


</style>
