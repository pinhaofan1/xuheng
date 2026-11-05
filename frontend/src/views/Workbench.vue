<template>
  <div class="workbench">
    <!-- Modified for Xuheng: open review separately to preserve the editor. -->
    <StatsTopBar :slug="slug" @open-settings="appSettingsShell.open()">
      <template #brand>
        <router-link to="/" class="workspace-brand" aria-label="返回作品库"><BrandMark :size="27" /><span>叙衡</span></router-link>
        <span class="workspace-divider" />
      </template>
      <template #actions>
        <button class="workspace-tab active" type="button" @click="workAreaRef?.ensureAssistedMode()">章节创作</button>
        <n-button v-if="reviewChapter" tag="a" size="small" quaternary :href="`/book/${slug}/review/${reviewChapter}`" target="_blank" rel="noopener noreferrer">一致性审校 ↗</n-button>
        <button class="workspace-tab" type="button" @click="openReferencePanel('bible')">人物设定</button>
        <n-dropdown trigger="click" :options="workspaceTools" @select="openWorkspaceTool"><button class="workspace-tab workspace-more" type="button">更多工具<AppIcon name="chevron" :size="12" /></button></n-dropdown>
      </template>
    </StatsTopBar>

    <n-spin :show="pageLoading" class="workbench-spin" description="加载工作台…">
      <div class="workbench-inner">
        <n-split
          direction="horizontal"
          :min="WORKBENCH_SPLIT.sidebarMin"
          :max="WORKBENCH_SPLIT.sidebarMax"
          :default-size="WORKBENCH_SPLIT.sidebarDefault"
        >
          <template #1>
            <ChapterList
              ref="chapterListRef"
              :slug="slug"
              :chapters="chapters"
              :current-chapter-id="currentChapterId"
              :generation-prefs="generationPrefs"
              :writing-chapter-number="writingChapterNumber"
              :writing-pipeline-step="writingPipelineStep"
              @select="onSidebarChapterSelect"
              @back="goHome"
              @refresh="handleChapterUpdated"
              @plan-act="handlePlanAct"
            />
          </template>

          <template #2>
            <div class="wb-main-split" :class="{ 'wb-right-collapsed': rightCollapsed }">
              <n-split
                direction="horizontal"
                :min="WORKBENCH_SPLIT.mainMin"
                :max="WORKBENCH_SPLIT.mainMax"
                :default-size="WORKBENCH_SPLIT.mainDefault"
              >
                <template #1>
                  <WorkArea
                    ref="workAreaRef"
                    :slug="slug"
                    :book-title="bookTitle"
                    :chapters="chapters"
                    :current-chapter-id="currentChapterId"
                    :chapter-content="chapterContent"
                    :chapter-loading="chapterLoading"
                    :generation-prefs="generationPrefs"
                    @chapter-updated="handleChapterUpdated"
                    @select-chapter="handleChapterSelect"
                  />
                </template>

                <template #2>
                  <div v-if="rightCollapsed" class="wb-right-strip" @click="toggleRight">
                    <span class="wb-strip-icon">◀</span>
                  </div>
                  <SettingsPanel
                    v-else
                    :slug="slug"
                    :current-panel="rightPanel"
                    :current-chapter="currentChapter"
                    :generation-prefs="generationPrefs"
                    @update:current-panel="onSettingsPanelChange"
                    @collapse="toggleRight"
                  />
                </template>
              </n-split>
            </div>
          </template>
        </n-split>
      </div>
    </n-spin>

    <!-- 幕→章 AI 规划弹层 -->
    <ActPlanningModal
      v-model:show="showActPlanning"
      :act-id="actPlanningId"
      :act-title="actPlanningTitle"
      @confirmed="handleChapterUpdated"
    />
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, computed, ref, watch, defineAsyncComponent, type ComponentPublicInstance } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import { useDebouncedTask } from '../composables/useDebouncedTask'
import { useWorkbench } from '../composables/useWorkbench'
import { useStatsStore } from '../stores/statsStore'
import { useWorkbenchRefreshStore } from '../stores/workbenchRefreshStore'
import { useAppSettingsShellStore } from '../stores/appSettingsShellStore'
import StatsTopBar from '../components/stats/StatsTopBar.vue'
import BrandMark from '@/components/brand/BrandMark.vue'
import AppIcon from '@/components/layout/AppIcon.vue'
import ChapterList from '../components/workbench/ChapterList.vue'
import WorkArea from '../components/workbench/WorkArea.vue'
import SettingsPanel from '../components/workbench/SettingsPanel.vue'
import {
  WORKBENCH_CHAPTER_DESK_CHANGE_EVENT,
  WORKBENCH_OPEN_SETTINGS_PANEL_EVENT,
  WORKBENCH_GENERATION_PREFS_UPDATED_EVENT,
  isWorkbenchSettingsPanelName,
} from '../workbench/deskEvents'
import { WORKBENCH_SPLIT } from '../design/layoutDensity'
import { storageKeys } from '@/config/storageKeys'
import { runtimePerformance } from '@/config/performance'
import { readStorageBoolean, writeStorageBoolean } from '@/utils/storage'

const ActPlanningModal = defineAsyncComponent(() => import('../components/workbench/ActPlanningModal.vue'))

const route = useRoute()
const router = useRouter()
const message = useMessage()
const statsStore = useStatsStore()
const workbenchRefresh = useWorkbenchRefreshStore()
const appSettingsShell = useAppSettingsShellStore()

const slug = computed(() => String(route.params.slug ?? ''))

const chapterListRef = ref<ComponentPublicInstance<{ refreshStoryTree: () => void }> | null>(null)
const workAreaRef = ref<ComponentPublicInstance<{
  ensureAssistedMode: () => void
  streamingChapterNumber: import('vue').Ref<number | null>
  writingPipelineStep: import('vue').ComputedRef<number | null>
}> | null>(null)

const writingChapterNumber = computed(() => workAreaRef.value?.streamingChapterNumber?.value ?? null)
const writingPipelineStep = computed(() => workAreaRef.value?.writingPipelineStep?.value ?? null)

async function onSidebarChapterSelect(chapterId: number, title = '') {
  await handleChapterSelect(chapterId, title)
  workAreaRef.value?.ensureAssistedMode?.()
}

async function runChapterDeskReload() {
  await loadDesk()
  void statsStore.loadBookStats(slug.value, true).catch(() => {})
  window.dispatchEvent(new CustomEvent('plotpilot:bible-panel:soft-reload'))
  chapterListRef.value?.refreshStoryTree?.()
  workbenchRefresh.bumpAfterChapterDeskChange()
}

/** 合并短时间内的多次「整桌刷新」：全托管状态抖动 / 多源 emit 时只拉一次 API，减轻闪烁与日志刷屏 */
const chapterDeskReload = useDebouncedTask(
  runChapterDeskReload,
  () => runtimePerformance.workbench.deskReloadDebounceMs,
  {
    onError: () => {
      message.error('刷新工作台失败，请检查网络与后端是否已启动')
    },
  },
)

const handleChapterUpdated = () => {
  chapterDeskReload.schedule()
}

function onDeskChangeSignalFromPanels() {
  handleChapterUpdated()
}

function onOpenSettingsPanelFromChild(e: Event) {
  const panel = (e as CustomEvent<{ panel?: string }>).detail?.panel
  if (typeof panel === 'string' && isWorkbenchSettingsPanelName(panel)) {
    rightPanel.value = panel
  }
}

// 幕→章 规划弹层
const showActPlanning = ref(false)
const actPlanningId = ref('')
const actPlanningTitle = ref('')

const handlePlanAct = (actId: string, actTitle: string) => {
  actPlanningId.value = actId
  actPlanningTitle.value = actTitle
  showActPlanning.value = true
}

const rightCollapsed = ref(readStorageBoolean(storageKeys.workbenchRightPanelCollapsed))

function toggleRight() {
  rightCollapsed.value = !rightCollapsed.value
  writeStorageBoolean(storageKeys.workbenchRightPanelCollapsed, rightCollapsed.value)
}

const {
  bookTitle,
  chapters,
  generationPrefs,
  rightPanel,
  pageLoading,
  bookMeta,
  currentJobId,
  currentChapterId,
  chapterContent,
  chapterLoading,
  setRightPanel,
  loadDesk,
  reloadDeskForSlugChange,
  goHome,
  goToChapter,
  handleChapterSelect,
} = useWorkbench({ slug })

const currentChapter = computed(() => {
  if (!currentChapterId.value) return null
  return chapters.value.find(ch => ch.id === currentChapterId.value) || null
})
const reviewChapter = computed(() => currentChapterId.value || chapters.value[0]?.number)
const workspaceTools = [
  { label: '任务与用量', key: 'operations' },
  { label: '世界观', key: 'worldbuilding' }, { label: '知识库', key: 'knowledge' },
  { label: '伏笔账本', key: 'foreshadow' }, { label: '故事演进', key: 'story-evolution' },
  { label: '角色档案', key: 'sandbox' }, { type: 'divider', key: 'divider' },
  { label: '人物关系图', key: 'characters' }, { label: '地点关系图', key: 'location-graph' },
]
function openReferencePanel(panel: string) { rightCollapsed.value = false; onSettingsPanelChange(panel) }
function openWorkspaceTool(key: string) {
  if (key === 'characters' || key === 'location-graph' || key === 'operations') void router.push(`/book/${slug.value}/${key}`)
  else openReferencePanel(key)
}
function syncPanelFromRoute() {
  const panel = route.query.panel
  if (typeof panel === 'string' && isWorkbenchSettingsPanelName(panel)) openReferencePanel(panel)
}

function onSettingsPanelChange(panel: string) {
  rightPanel.value = panel
}

function parseChapterQuery(q: unknown): number | null {
  if (q == null || q === '') return null
  const raw = Array.isArray(q) ? q[0] : q
  const n = Number(raw)
  return Number.isInteger(n) && n >= 1 ? n : null
}

async function syncChapterFromRoute() {
  const n = parseChapterQuery(route.query.chapter) ?? chapters.value[0]?.number
  if (n != null) {
    await goToChapter(n)
  }
}

function onGenerationPrefsUpdated() {
  void loadDesk()
  chapterListRef.value?.refreshStoryTree?.()
}

onMounted(async () => {
  window.addEventListener(WORKBENCH_CHAPTER_DESK_CHANGE_EVENT, onDeskChangeSignalFromPanels)
  window.addEventListener(WORKBENCH_OPEN_SETTINGS_PANEL_EVENT, onOpenSettingsPanelFromChild)
  window.addEventListener(WORKBENCH_GENERATION_PREFS_UPDATED_EVENT, onGenerationPrefsUpdated)
  try {
    await loadDesk()
    await syncChapterFromRoute()
    syncPanelFromRoute()
  } catch {
    message.error('加载失败，请检查网络与后端是否已启动')
    bookTitle.value = slug.value
  } finally {
    pageLoading.value = false
  }
})

onUnmounted(() => {
  window.removeEventListener(WORKBENCH_CHAPTER_DESK_CHANGE_EVENT, onDeskChangeSignalFromPanels)
  window.removeEventListener(WORKBENCH_OPEN_SETTINGS_PANEL_EVENT, onOpenSettingsPanelFromChild)
  window.removeEventListener(WORKBENCH_GENERATION_PREFS_UPDATED_EVENT, onGenerationPrefsUpdated)
  chapterDeskReload.cancel()
})

watch(
  () => route.query.chapter,
  () => {
    void syncChapterFromRoute()
  }
)
watch(() => route.query.panel, syncPanelFromRoute)

watch(
  slug,
  async (next, prev) => {
    if (!next || prev === next) return
    try {
      await reloadDeskForSlugChange()
      await syncChapterFromRoute()
      syncPanelFromRoute()
      void statsStore.loadBookStats(next, true).catch(() => {})
      chapterListRef.value?.refreshStoryTree?.()
      workbenchRefresh.bumpAfterChapterDeskChange()
    } catch {
      message.error('切换作品失败，请检查网络与后端是否已启动')
      bookTitle.value = next
    }
  }
)
</script>

<style scoped>
.workspace-brand{display:flex;align-items:center;gap:9px;color:var(--app-text-primary);text-decoration:none;font-size:18px;font-weight:600;letter-spacing:2px;white-space:nowrap}.workspace-divider{height:23px;width:1px;background:var(--app-border);margin:0 8px}.workspace-tab{border:0;background:none;color:var(--app-text-secondary);font:500 11px var(--font-sans);padding:9px 10px;cursor:pointer;border-radius:5px;white-space:nowrap}.workspace-tab:hover{background:var(--app-surface-subtle)}.workspace-tab.active{color:var(--color-brand);background:var(--color-brand-light)}.workspace-more{display:flex;align-items:center;gap:4px}.workspace-more svg{transform:rotate(90deg)}.workspace-tab:focus-visible,.workspace-brand:focus-visible{outline:2px solid var(--color-brand);outline-offset:2px}
@media(max-width:1100px){.workspace-divider{margin:0 3px}.workspace-brand{font-size:16px}.workspace-tab{padding:7px 5px;font-size:10px}.workspace-more{font-size:10px}}@media(max-width:760px){.workspace-divider{display:none}.workspace-brand>span{display:none}.workspace-tab{padding:5px}.workspace-tab:not(.workspace-more):not(.active){display:none}}
.workbench {
  --wb-paper: var(--app-surface);
  --wb-paper-muted: var(--app-surface-subtle);
  --wb-paper-raised: var(--app-surface);
  --wb-line: var(--app-border-strong);
  --wb-line-soft: var(--app-border);
  --wb-shadow-card: 0 1px 3px #26355606;
  height: 100vh;
  min-height: 0;
  max-height: 100vh;
  overflow: hidden;
  background: var(--app-page-bg, #f0f2f8);
  display: flex;
  flex-direction: column;
}

.workbench-spin {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.workbench-spin :deep(.n-spin-content) {
  flex: 1;
  min-height: 0;
  height: auto;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.workbench-inner {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.workbench-inner :deep(.n-split) {
  flex: 1;
  min-height: 0;
  height: 100%;
}

.workbench-inner :deep(.n-split-pane-1),
.workbench-inner :deep(.n-split-pane-2) {
  min-height: 0;
  overflow: hidden;
}

/* ── Right sidebar collapse ─────────────────────────── */

.wb-main-split {
  height: 100%;
  width: 100%;
  overflow: hidden;
}

.wb-right-collapsed :deep(.n-split-pane-1) {
  flex: 1 1 0 !important;
  width: 0 !important;
  max-width: none !important;
}

.wb-right-collapsed :deep(.n-split-pane-2) {
  flex: 0 0 32px !important;
  width: 32px !important;
  min-width: 0 !important;
  max-width: 32px !important;
  overflow: hidden;
}

.wb-right-collapsed :deep(.n-split__gutter) {
  display: none !important;
}

.wb-right-strip {
  height: 100%;
  width: 32px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  background: var(--app-surface);
  border-left: 1px solid var(--plotpilot-split-border);
  color: var(--app-text-muted);
  font-size: 12px;
  transition: background 0.15s, color 0.15s;
  user-select: none;
}

.wb-right-strip:hover {
  background: var(--plotpilot-panel-muted);
  color: var(--app-text-primary);
}
</style>
