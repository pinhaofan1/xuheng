<template>
  <div class="home">
    <WorkspaceNav section="library" @create="openCreateModal" @refresh="handleRefreshList" />
    <main class="home-content">
      <header class="library-topbar"><span>我的工作空间</span><div><span class="local-workspace"><i />本地工作空间</span><n-button quaternary aria-label="应用设置" @click="appSettingsShell.open()"><AppIcon name="settings" /></n-button></div></header>
      <div class="library-content">
        <header class="library-heading"><div><p class="library-eyebrow">创作，从这里继续</p><h1>作品库</h1><p>组织故事与人物，让每一次修改都有据可循。</p></div><n-button type="primary" @click="openCreateModal"><template #icon><AppIcon name="plus" /></template>新建作品</n-button></header>
        <section class="library-overview" aria-label="作品概览"><div><AppIcon name="library" /><span>全部作品</span><strong>{{ books.length }}<small> 部</small></strong></div><div><AppIcon name="book" /><span>已有章节</span><strong>{{ books.reduce((sum, book) => sum + (book.chapter_count || 0), 0) }}<small> 章</small></strong></div><div><AppIcon name="write" /><span>累计创作</span><strong>{{ books.reduce((sum, book) => sum + (book.word_count || 0), 0).toLocaleString('zh-CN') }}<small> 字</small></strong></div></section>
        <section class="books-section" aria-labelledby="library-list-heading">
          <div class="section-header"><div><h2 id="library-list-heading">我的作品 <span>{{ filteredBooks.length }}</span></h2><p>进入创作工作台，或直接审校已保存的章节。</p></div><div class="section-tools"><n-input v-model:value="searchQuery" placeholder="搜索作品名称或类型" clearable :input-props="{ 'aria-label': '搜索作品' }"><template #prefix><AppIcon name="search" :size="16" /></template></n-input><n-button quaternary @click="handleRefreshList" aria-label="刷新作品列表"><AppIcon name="history" :size="18" /></n-button><n-button secondary @click="showLibrary = true">管理作品</n-button></div></div>
          <div v-if="loading" class="library-loading"><n-spin /><span>正在加载作品…</span></div>
          <div v-else-if="!books.length" class="library-empty"><AppIcon name="book" :size="42" /><h3>写下你的第一个故事</h3><p>创建作品，整理人物和世界，再开始第一章。</p><n-button type="primary" @click="openCreateModal">新建作品</n-button></div>
          <div v-else-if="!filteredBooks.length" class="library-empty"><AppIcon name="search" :size="32" /><h3>没有找到匹配的作品</h3><n-button text type="primary" @click="searchQuery = ''">清除搜索</n-button></div>
          <BookLibrary v-else :books="filteredBooks" :busy="Boolean(deletingSlug)" @open="navigateToBook" @delete="requestDeleteBooks([$event])" />
        </section>
        <section class="workspace-guide"><AppIcon name="review" :size="22"/><div><h3>从创作到审校，不必切换工具</h3><p>章节编辑、人物设定、伏笔与历史依据，都可以在作品工作台中找到。</p></div><router-link to="/about">了解叙衡<AppIcon name="arrow" :size="16"/></router-link></section>
        <footer class="home-footer"><span>叙衡 · 故事有序，创作有据</span><span>作品保存在本机</span></footer>
      </div>
    </main>

    <!-- 新建书目弹层：大表单从主页搬进对话框，主页面保持一屏（header + 书目网格） -->
    <n-modal
      v-model:show="createModalOpen"
      preset="card"
      class="create-modal"
      :style="{ width: 'min(760px, 94vw)' }"
      :bordered="false"
      :mask-closable="!creating"
      :close-on-esc="!creating"
      :segmented="{ footer: 'soft' }"
    >
      <template #header>
        <div class="create-title-wrap">
          <n-icon :component="IconSpark" :size="18" class="create-icon" />
          <span class="create-title">新建书目</span>
        </div>
      </template>
      <div class="create-modal-body">
        <n-space vertical :size="20">
          <n-input
            ref="createInputRef"
            v-model:value="newBook.premise"
            type="textarea"
            placeholder="用一段话写清主线与爽点预期（不超过 2000 字）…&#10;&#10;例如：废柴赘婿觉醒签到系统，从被退婚到一方巨擘。"
            :rows="5"
            :disabled="creating"
            size="large"
            class="premise-input"
            show-count
            :maxlength="PREMISE_MAX_LEN"
          />

          <div class="taxonomy-block">
            <div class="taxonomy-block-head">
              <span class="taxonomy-block-title">市场分区</span>
              <span class="taxonomy-block-sub">大类 → 细分主题 → 自动写入「类型 / 世界观」；均可再改。</span>
            </div>
            <MarketTaxonomyPicker
              v-model:genre="newBook.genre"
              v-model:worldPreset="newBook.worldPreset"
              v-model:storyStructure="newBook.storyStructure"
              v-model:pacingControl="newBook.pacingControl"
              v-model:writingStyle="newBook.writingStyle"
              v-model:specialRequirements="newBook.specialRequirements"
              :disabled="creating"
            />
          </div>

          <div v-show="!showAdvanced" class="length-tier-block">
            <div class="length-tier-label">目标篇幅（选一个即可，系统按网文常用节奏推导章数）</div>
            <n-radio-group v-model:value="lengthTier" name="lengthTier" class="length-tier-group">
              <n-space :size="14" :wrap="true" align="flex-start" class="length-tier-space">
                <n-radio
                  v-for="opt in lengthTierOptions"
                  :key="opt.value"
                  :value="opt.value"
                  :disabled="creating"
                  class="length-tier-radio"
                >
                  <div class="length-tier-option-inner">
                    <span class="length-tier-title">{{ opt.title }}</span>
                    <span class="length-tier-hint">{{ opt.hint }}</span>
                  </div>
                </n-radio>
              </n-space>
            </n-radio-group>
          </div>

          <div v-show="showAdvanced" class="advanced-settings">
            <n-alert type="info" :show-icon="true" style="margin-bottom: 12px; font-size: 12px">
              自定义章数与每章字数时，不再使用「目标篇幅」档位推导；结构提示仍会在后台写入梗概供模型使用。
            </n-alert>
            <n-grid :cols="2" :x-gap="16" :y-gap="16" responsive="screen">
              <n-gi>
                <n-form-item label="书名">
                  <n-input v-model:value="newBook.title" placeholder="留空则从梗概自动截取" />
                </n-form-item>
              </n-gi>
              <n-gi>
                <n-form-item label="章节数">
                  <n-input-number v-model:value="newBook.chapters" :min="1" :max="9999" class="w-full" placeholder="默认 100 章" />
                </n-form-item>
              </n-gi>
              <n-gi>
                <n-form-item label="每章字数">
                  <n-input-number v-model:value="newBook.words" :min="500" :max="20000" :step="500" class="w-full" />
                </n-form-item>
              </n-gi>
            </n-grid>
          </div>
        </n-space>
      </div>
      <template #action>
        <div class="create-modal-footer">
          <n-button text type="primary" :disabled="creating" @click="showAdvanced = !showAdvanced">
            <template #icon>
              <n-icon><component :is="showAdvanced ? IconChevronUp : IconChevronDown" /></n-icon>
            </template>
            {{ showAdvanced ? '收起高级' : '高级（自定义章数/每章字数）' }}
          </n-button>
          <n-button
            type="primary"
            size="large"
            round
            :loading="creating"
            :disabled="!newBook.premise.trim() || !newBook.genre.trim() || !newBook.worldPreset.trim() || !newBook.storyStructure.trim() || !newBook.pacingControl.trim() || !newBook.writingStyle.trim() || !newBook.specialRequirements.trim()"
            @click="handleCreate"
          >
            <template #icon>
              <n-icon><IconSpark /></n-icon>
            </template>
            建档并进入工作台
          </n-button>
        </div>
      </template>
    </n-modal>

    <!-- 书目录弹层（自 pw fe HomeLibraryModal 移植）：搜索/批量管理/删除 -->
    <LibraryModal
      v-model:show="showLibrary"
      :books="books"
      :deleting-slug="deletingSlug"
      @open="navigateToBook"
      @request-delete="requestDeleteBooks"
      @new-book="openCreateModal"
    />

    <!-- 删除确认（书架卡片/目录弹层触发，支持单本与批量） -->
    <n-modal
      v-model:show="showDeleteConfirm"
      preset="confirm"
      type="error"
      :title="`删除书目${pendingDeleteBooks.length > 1 ? `（${pendingDeleteBooks.length} 本）` : ''}`"
    >
      <template #default>
        <template v-if="pendingDeleteBooks.length === 1">
          将删除 <strong>《{{ pendingDeleteBooks[0].title }}》</strong> 及本地全部章节与设定，此操作不可恢复。
        </template>
        <template v-else>
          将删除 <strong>{{ pendingDeleteBooks.length }}</strong> 本书（{{
            pendingDeleteBooks.slice(0, 3).map(b => `《${b.title}》`).join('、')
          }}<template v-if="pendingDeleteBooks.length > 3">等</template>）及各自全部章节与设定，此操作不可恢复。
        </template>
      </template>
      <template #action>
        <n-space>
          <n-button @click="showDeleteConfirm = false">取消</n-button>
          <n-button type="error" :loading="deleting" @click="confirmDeleteBooks">确认删除</n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- 新书向导：仅挂载一次且 show 恒为 true，避免「先关再开」的双过渡（原 newNovelId + showSetupGuide 分步更新导致） -->
    <NovelSetupGuide
      v-if="setupWizard"
      :key="setupWizard.novelId"
      :novel-id="setupWizard.novelId"
      :target-chapters="setupWizard.targetChapters"
      :show="true"
      @update:show="(open) => { if (!open) setupWizard = null }"
      @complete="handleSetupComplete"
      @skip="handleSetupSkip"
    />

  </div>
</template>

<script setup lang="ts">
import { defineAsyncComponent, h, ref, onMounted, computed, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useMessage, NIcon } from 'naive-ui'
import { novelApi, type NovelDTO } from '../api/novel'
import { isWizardCompleted } from '@/utils/wizardStageCache'
import WorkspaceNav from '@/components/layout/WorkspaceNav.vue'
import AppIcon from '@/components/layout/AppIcon.vue'
import BookLibrary, { type LibraryBook } from '@/components/home/BookLibrary.vue'
import LibraryModal from '@/components/home/LibraryModal.vue'
import { useAppSettingsShellStore } from '@/stores/appSettingsShellStore'
import { parseGenreWorldFromPremise } from '@/utils/premisePresets'
import { useStatsStore } from '@/stores/statsStore'
import { formatApiError } from '@/utils/apiError'
import {
  NOVEL_LENGTH_TIER_OPTIONS,
  getNovelStageLabel,
  type NovelLengthTier,
} from '@/domain/novel'

const MarketTaxonomyPicker = defineAsyncComponent(
  () => import('@/components/taxonomy/MarketTaxonomyPicker.vue'),
)
const NovelSetupGuide = defineAsyncComponent(
  () => import('@/components/onboarding/NovelSetupGuide.vue'),
)

// Icons
const IconSpark = () =>
  h('svg', { xmlns: 'http://www.w3.org/2000/svg', viewBox: '0 0 24 24', width: '1em', height: '1em' },
    h('path', { fill: 'currentColor', d: 'M13 2L3 14h8l-1 8 10-12h-8l1-8z' }))

const IconLibrary = () =>
  h('svg', { xmlns: 'http://www.w3.org/2000/svg', viewBox: '0 0 24 24', width: '1em', height: '1em' },
    h('path', { fill: 'currentColor', d: 'M4 6H2v14c0 1.1.9 2 2 2h14v-2H4V6zm16-4H8c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-9 12V6h7v2h-5v2h5v2h-5v2h5v2h-7z' }))

const IconSearch = () =>
  h('svg', { xmlns: 'http://www.w3.org/2000/svg', viewBox: '0 0 24 24', width: '1em', height: '1em' },
    h('path', { fill: 'currentColor', d: 'M15.5 14h-.79l-.28-.27A6.471 6.471 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z' }))

const IconChevronDown = () =>
  h('svg', { xmlns: 'http://www.w3.org/2000/svg', viewBox: '0 0 24 24', width: '1em', height: '1em' },
    h('path', { fill: 'currentColor', d: 'M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6 1.41-1.41z' }))

const IconChevronUp = () =>
  h('svg', { xmlns: 'http://www.w3.org/2000/svg', viewBox: '0 0 24 24', width: '1em', height: '1em' },
    h('path', { fill: 'currentColor', d: 'M7.41 15.41L12 10.83l4.59 4.58L18 14l-6-6-6 6 1.41 1.41z' }))

/** 与工作台顶栏一致：打开应用设置（默认「外观与主题」分区） */
const IconThemeSettings = () =>
  h('svg', { xmlns: 'http://www.w3.org/2000/svg', viewBox: '0 0 24 24', width: '1em', height: '1em' },
    h('path', {
      fill: 'currentColor',
      d: 'M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32a.49.49 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 0 0-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96a.49.49 0 0 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 0 0-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6A3.6 3.6 0 1 1 12 8.4a3.6 3.6 0 0 1 0 7.2z',
    }))

interface BookListItem extends LibraryBook {}

const router = useRouter()
const message = useMessage()
const statsStore = useStatsStore()
const appSettingsShell = useAppSettingsShellStore()

const createInputRef = ref<any>(null)
const showAdvanced = ref(false)
const creating = ref(false)
const loading = ref(false)

const books = ref<BookListItem[]>([])
const searchQuery = ref('')
const deletingSlug = ref<string | null>(null)
/** 删除中（批量删除时确认按钮转圈） */
const deleting = ref(false)
/** 删除确认弹层（书架卡片/目录弹层触发） */
const showDeleteConfirm = ref(false)
const pendingDeleteBooks = ref<BookListItem[]>([])
/** 全部书目（书目录）弹层：书架管「当下在读」，这里管全部藏书 */
const showLibrary = ref(false)
/** 新建书目弹层开关：大表单已从主页搬进对话框，主页保持一屏 */
const createModalOpen = ref(false)
/** 有值时挂载向导；与 show 分离，挂载后始终 :show="true"，避免 Modal 先 false 再 true 闪烁 */
const setupWizard = ref<{ novelId: string; targetChapters: number } | null>(null)

const PREMISE_MAX_LEN = 2000

const newBook = ref({
  title: '',
  premise: '',
  genre: '',
  worldPreset: '',
  storyStructure: '',
  pacingControl: '',
  writingStyle: '',
  specialRequirements: '',
  chapters: 100,  // 默认 100 章
  words: 2500,
})

/** V1 目标篇幅档（与高级自定义二选一） */
const lengthTier = ref<NovelLengthTier>('standard')
const lengthTierOptions = NOVEL_LENGTH_TIER_OPTIONS

const filteredBooks = computed(() => {
  if (!searchQuery.value.trim()) {
    return books.value
  }
  const query = searchQuery.value.toLowerCase()
  return books.value.filter(
    book =>
      book.title.toLowerCase().includes(query) ||
      (book.genre && book.genre.toLowerCase().includes(query))
  )
})

const fetchBooks = async () => {
  loading.value = true
  try {
    const novels = await novelApi.listNovels()
    books.value = novels.map((novel: NovelDTO) => {
      const fromPrefix = parseGenreWorldFromPremise(novel.premise || '').genre
      const g = novel.locked_genre?.trim() || fromPrefix || ''
      return {
        slug: novel.id,
        title: novel.title,
        stage: novel.stage,
        stage_label: getNovelStageLabel(novel.stage),
        genre: g,
        premise: (novel.premise || '').trim(),
        chapter_count: novel.chapters?.length || 0,
        target_chapters: novel.target_chapters || 0,
        word_count: novel.total_word_count,
      }
    })
  } catch {
    message.error('加载失败')
  } finally {
    loading.value = false
  }
}

const handleCreate = async () => {
  if (!newBook.value.premise.trim()) {
    message.warning('请输入核心梗概')
    return
  }
  if (!newBook.value.genre.trim()) {
    message.warning('请在「市场分区」中选定大类与主题')
    return
  }
  if (!newBook.value.worldPreset.trim()) {
    message.warning('请填写或确认世界观基调')
    return
  }
  if (!newBook.value.storyStructure.trim() || !newBook.value.pacingControl.trim() || !newBook.value.writingStyle.trim() || !newBook.value.specialRequirements.trim()) {
    message.warning('请补全四项写作规则')
    return
  }

  creating.value = true
  try {
    const title = newBook.value.title || newBook.value.premise.substring(0, 20)
    const novelId = `novel-${Date.now()}`

    const base = {
      novel_id: novelId,
      title: title,
      author: '作者',
      premise: newBook.value.premise.trim(),
      genre: newBook.value.genre,
      world_preset: newBook.value.worldPreset,
      story_structure: newBook.value.storyStructure,
      pacing_control: newBook.value.pacingControl,
      writing_style: newBook.value.writingStyle,
      special_requirements: newBook.value.specialRequirements,
    }
    const result = await novelApi.createNovel(
      showAdvanced.value
        ? {
            ...base,
            target_chapters: newBook.value.chapters || 100,
            target_words_per_chapter: newBook.value.words || 2500,
          }
        : {
            ...base,
            length_tier: lengthTier.value,
            target_chapters: 0,
          }
    )
    message.success('创建成功')
    // 成功即关弹层，后续 setupWizard 弹层接管流程
    createModalOpen.value = false

    setupWizard.value = {
      novelId: result.id,
      targetChapters: result.target_chapters,
    }
  } catch (error: unknown) {
    message.error(formatApiError(error, '创建失败'))
  } finally {
    creating.value = false
  }
}

const handleSetupComplete = () => {
  const id = setupWizard.value?.novelId
  setupWizard.value = null
  if (id) router.push(`/book/${id}/workbench`)
}

const handleSetupSkip = () => {
  const id = setupWizard.value?.novelId
  setupWizard.value = null
  if (id) router.push(`/book/${id}/workbench`)
}

const navigateToBook = (novelId: string) => {
  // 未完成向导的书重新打开向导
  if (!isWizardCompleted(novelId) && !(books.value.find(book => book.slug === novelId)?.chapter_count)) {
    setupWizard.value = {
      novelId,
      targetChapters: 100, // 默认值，向导内部会从 API 获取真实值
    }
    return
  }
  router.push(`/book/${novelId}/workbench`)
}

/** 删除入口（书架卡片单本 / 目录弹层单本或批量）：先弹确认框 */
const requestDeleteBooks = (slugs: string[]) => {
  const unique = Array.from(new Set(slugs.filter(Boolean)))
  pendingDeleteBooks.value = books.value.filter(b => unique.includes(b.slug))
  if (!pendingDeleteBooks.value.length) return
  showDeleteConfirm.value = true
}

const confirmDeleteBooks = async () => {
  if (!pendingDeleteBooks.value.length) return
  const pendingSlugs = new Set(pendingDeleteBooks.value.map(b => b.slug))
  deleting.value = true
  // 1. 逐本串行删除：后端无批量接口；失败不中断，最后统一报数
  let successCount = 0
  let failCount = 0
  const failed: BookListItem[] = []
  for (const book of pendingDeleteBooks.value) {
    deletingSlug.value = book.slug
    try {
      await novelApi.deleteNovel(book.slug)
      successCount += 1
    } catch {
      failCount += 1
      failed.push(book)
    }
  }
  // 2. 本地列表只移走删成功的；失败的留在架上（防止误清整个书架）
  const failedSlugs = new Set(failed.map(b => b.slug))
  if (successCount) {
    books.value = books.value.filter(
      b => failedSlugs.has(b.slug) || !pendingSlugs.has(b.slug)
    )
    await statsStore.loadGlobalStats(true)
  }
  deletingSlug.value = null
  pendingDeleteBooks.value = []
  showDeleteConfirm.value = false
  if (successCount) message.success(`已删除 ${successCount} 本书`)
  if (failCount) message.warning(`${failCount} 本书删除失败`)
  deleting.value = false
}

/** 打开新建书目弹层（侧边栏/空态/区头按钮共用入口） */
const openCreateModal = () => {
  createModalOpen.value = true
}

// 弹层打开后聚焦梗概输入框，保留原直达手感
watch(createModalOpen, (open) => {
  if (!open) return
  nextTick(() => {
    createInputRef.value?.focus()
  })
})

const handleRefreshList = async () => {
  await fetchBooks()
  message.success('列表已刷新')
}

onMounted(() => {
  fetchBooks()
})
</script>

<style scoped>
.books-section{background:transparent;
  border:0;
  box-shadow:none;
  padding:0;
  border-radius:0;
  }

.home{min-height:100vh;
  background:var(--app-page-bg);
  color:var(--app-text-primary)}
.home-content{margin-left:216px;
  min-width:0}
.library-topbar{height:65px;
  background:var(--app-surface);
  border-bottom:1px solid var(--app-border);
  display:flex;
  align-items:center;
  justify-content:space-between;
  padding:0 38px;
  font-size:12px;
  color:var(--app-text-muted)}
.library-topbar>div{display:flex;
  gap:18px;
  align-items:center}
.local-workspace{display:flex;
  gap:7px;
  align-items:center;
  font-size:11px}
.local-workspace i{width:6px;
  height:6px;
  background:#3c9182;
  border-radius:50%}
.library-content{max-width:1370px;
  margin:0 auto;
  padding:42px 42px 24px}
.library-heading{display:flex;
  justify-content:space-between;
  align-items:center;
  margin-bottom:30px;
  gap:20px}
.library-eyebrow{font-size:11px;
  color:var(--app-text-muted);
  margin-bottom:7px;
  letter-spacing:.05em}
.library-heading h1{font-size:29px;
  font-weight:650;
  letter-spacing:-.03em;
  margin:0 0 9px}
.library-heading p:last-child{font-size:12px;
  color:var(--app-text-muted)}
.library-heading :deep(.n-button){height:37px;
  padding:0 18px}
.library-overview{display:grid;
  grid-template-columns:repeat(3,1fr);
  gap:22px;
  margin-bottom:38px}
.library-overview>div{border:1px solid var(--app-border);
  border-radius:9px;
  background:var(--app-surface);
  padding:18px 20px;
  display:flex;
  align-items:center;
  gap:10px}
.library-overview svg{color:var(--app-text-muted)}
.library-overview span{font-size:11px;
  color:var(--app-text-muted)}
.library-overview strong{font-size:23px;
  margin-left:auto;
  font-weight:600;
  font-variant-numeric:tabular-nums}
.library-overview small{font-size:10px;
  font-weight:400;
  color:var(--app-text-muted)}
.section-header{display:flex;
  align-items:center;
  justify-content:space-between;
  gap:20px;
  margin-bottom:23px}
.section-header h2{font-size:16px;
  font-weight:600;
  margin:0 0 6px}
.section-header h2 span{font-size:11px;
  font-weight:500;
  background:var(--app-surface);
  border:1px solid var(--app-border);
  padding:1px 6px;
  border-radius:4px;
  margin-left:7px}
.section-header p{font-size:11px;
  color:var(--app-text-muted)}
.section-tools{display:flex;
  gap:8px;
  align-items:center}
.section-tools :deep(.n-input){width:220px}
.library-loading,.library-empty{padding:70px 20px;
  display:flex;
  align-items:center;
  justify-content:center;
  gap:16px;
  background:var(--app-surface);
  border:1px dashed var(--app-border-strong);
  border-radius:9px}
.library-empty{flex-direction:column;
  color:var(--app-text-muted);
  font-size:12px}
.library-empty h3{color:var(--app-text-primary);
  font-size:18px;
  font-weight:500}
.workspace-guide{margin-top:40px;
  display:flex;
  align-items:center;
  gap:16px;
  padding:21px 23px;
  border:1px solid var(--app-border);
  background:var(--app-surface-subtle);
  border-radius:9px;
  color:var(--app-text-muted)}
.workspace-guide>svg{color:var(--color-brand)}
.workspace-guide h3{font-size:12px;
  font-weight:500;
  color:var(--app-text-secondary);
  margin:0 0 5px}
.workspace-guide p{font-size:11px;
  line-height:1.8}
.workspace-guide a{margin-left:auto;
  white-space:nowrap;
  display:flex;
  align-items:center;
  gap:8px;
  text-decoration:none;
  color:var(--app-text-muted);
  font-size:11px}
.home-footer{display:flex;
  justify-content:space-between;
  font-size:10px;
  color:var(--app-text-muted);
  margin-top:30px;
  padding-top:18px}
.create-modal-body{padding-top:10px}
.create-title-wrap{display:flex;
  align-items:center;
  gap:8px}
.create-modal-footer{display:flex;
  align-items:center;
  justify-content:space-between;
  gap:14px}
.taxonomy-block{padding:16px;
  border:1px solid var(--app-border);
  border-radius:8px;
  background:var(--app-surface-subtle)}
.taxonomy-block-head{display:flex;
  flex-direction:column;
  gap:5px;
  margin-bottom:12px}
.taxonomy-block-title,.length-tier-label{font-size:12px;
  font-weight:600}
.taxonomy-block-sub,.length-tier-hint{font-size:11px;
  color:var(--app-text-muted)}
.length-tier-label{margin-bottom:12px}
.length-tier-option-inner{display:flex;
  flex-direction:column;
  gap:4px}
.length-tier-title{font-size:12px}
.w-full{width:100%}

@media(min-width:1650px){.library-content{padding-top:50px}
}
@media(max-width:1100px){.library-content{padding:30px 28px}
.library-topbar{padding:0 28px}
.section-header{align-items:flex-start;
  flex-direction:column}
.section-tools{width:100%}
.section-tools :deep(.n-input){flex:1;
  width:auto}
.library-overview{gap:10px}
.library-overview>div{padding:15px 12px;
  gap:6px}
.library-overview>div>svg{display:none}
.library-overview strong{font-size:20px}
}
@media(max-width:800px){.home-content{margin-left:0}
.library-topbar{padding-left:66px}
.library-content{padding:27px 20px}
.library-heading{margin-bottom:22px}
.library-heading h1{font-size:25px}
.library-heading p:last-child{max-width:200px;
  line-height:1.8}
.library-overview{gap:8px;
  margin-bottom:28px}
.library-overview>div{flex-wrap:wrap;
  gap:6px}
.library-overview strong{width:100%;
  margin:0;
  font-size:20px}
.workspace-guide{padding:18px;
  align-items:flex-start;
  gap:12px}
.workspace-guide a{display:none}
.workspace-guide>svg{flex-shrink:0}
.home-footer{font-size:9px}
.create-modal-footer{flex-wrap:wrap}
.library-heading :deep(.n-button){padding:0 12px}
.local-workspace{font-size:10px}
}


</style>
