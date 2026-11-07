<script setup lang="ts">
import { computed, ref } from 'vue'
import { chapterApi } from '@/api/chapter'
import { useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import { formatApiError } from '@/utils/apiError'
import AppIcon from '@/components/layout/AppIcon.vue'
export interface LibraryBook { slug: string; title: string; stage: string; stage_label: string; genre: string; premise: string; chapter_count?: number; target_chapters?: number; word_count?: number }
const props=defineProps<{books:LibraryBook[];busy?:boolean}>()
defineEmits<{open:[slug:string];delete:[slug:string]}>()
const router=useRouter(), message=useMessage()
const opening=ref('')
const total=computed(()=>props.books.length)
const cleanPremise=(text:string)=>text.replace(/【[^】]*】/g,'').trim() || '故事从这里开始。进入工作台完善人物、世界观与章节。'
async function reviewBook(book:LibraryBook){
 opening.value=book.slug
 try{
  const chapters=await chapterApi.listChapters(book.slug)
  const latest=[...chapters].sort((a,b)=>b.number-a.number)[0]
  if(!latest){message.info('请先在创作工作台保存一章正文，再开始审校');return}
  await router.push(`/book/${book.slug}/review/${latest.number}`)
 }catch(error){message.error(formatApiError(error,'无法打开章节，请稍后重试'))}
 finally{opening.value=''}
}
</script>
<template>
  <div class="book-library" :aria-label="`${total} 部作品`">
    <article v-for="(book, index) in books" :key="book.slug" class="library-book">
      <div class="book-folio" aria-hidden="true">
        <AppIcon name="book" :size="23" />
        <span>{{ String(index + 1).padStart(2, '0') }}</span>
      </div>

      <div class="book-information">
        <p class="book-label"><span>{{ book.stage_label || '创作中' }}</span><span v-if="book.genre">{{ book.genre }}</span></p>
        <h3><button type="button" @click="$emit('open', book.slug)">{{ book.title }}</button></h3>
        <p class="book-premise">{{ cleanPremise(book.premise) }}</p>
      </div>

      <div class="book-controls">
        <div class="book-metadata">
          <span>{{ book.chapter_count || 0 }}<template v-if="book.target_chapters"> / {{ book.target_chapters }}</template> 章</span>
          <span>{{ (book.word_count || 0).toLocaleString('zh-CN') }} 字</span>
          <n-dropdown trigger="click" :options="[{ label: '删除作品', key: 'delete' }]" @select="$emit('delete', book.slug)">
            <button type="button" class="book-more" :disabled="busy" :aria-label="`管理《${book.title}》`">
              <AppIcon name="more" />
            </button>
          </n-dropdown>
        </div>
        <div class="book-card-actions">
          <n-button secondary :disabled="busy" @click="$emit('open', book.slug)">
            <template #icon><AppIcon name="write" :size="16" /></template>
            继续创作
          </n-button>
          <n-button quaternary :disabled="busy || !!opening" @click="reviewBook(book)">
            <template #icon><AppIcon name="review" :size="16" /></template>
            {{ opening === book.slug ? '加载中…' : '审校' }}
          </n-button>
        </div>
      </div>
    </article>
  </div>
</template>

<style scoped>
.book-library { border-top: 1px solid var(--app-border); }

.library-book {
  display: grid;
  grid-template-columns: 76px minmax(0, 1fr) auto;
  align-items: center;
  gap: 24px;
  padding: 17px 24px;
  border-bottom: 1px solid var(--app-border);
  background: var(--app-surface);
  transition: background-color .16s;
}

.library-book:hover { background: var(--app-surface-subtle); }

.book-folio {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 16px;
  min-height: 104px;
  padding: 13px 11px;
  border-inline-start: 3px solid var(--color-brand);
  background: var(--color-brand-light);
  color: var(--color-brand);
  font: 500 22px/1 Georgia, serif;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}

.book-information,
.book-controls { min-width: 0; }

.book-label,
.book-metadata {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 14px;
  color: var(--app-text-secondary);
  font-size: 12px;
  margin: 0;
}

.book-label span:first-child {
  color: var(--color-brand);
  font-weight: 600;
}

.book-label span { overflow-wrap: anywhere; }

.book-information h3 {
  margin: 5px 0 7px;
  font: 700 clamp(19px, 2vw, 25px)/1.4 var(--xh-font-manuscript);
}

.book-information h3 button,
.book-more {
  border: 0;
  background: none;
  color: inherit;
  cursor: pointer;
}

.book-information h3 button {
  font: inherit;
  padding: 0;
  text-align: start;
  overflow-wrap: anywhere;
}

.book-information h3 button:hover { color: var(--color-brand); }

.book-premise {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  overflow-wrap: anywhere;
  margin: 0;
  color: var(--app-text-secondary);
  font-size: 13px;
  line-height: 1.75;
}

.book-controls {
  display: grid;
  justify-items: end;
  gap: 14px;
}

.book-metadata { font-variant-numeric: tabular-nums; }

.book-more {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border-radius: 4px;
}

.book-more:hover { background: var(--color-brand-light); }

.book-card-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.book-card-actions :deep(.n-button) { min-height: 36px; }

button:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 3px;
}

button:disabled { cursor: default; }

@media (max-width: 1080px) {
  .library-book { gap: 16px; padding: 16px; }
}

@media (max-width: 720px) {
  .library-book { grid-template-columns: 48px minmax(0, 1fr); }
  .book-folio { align-self: start; min-height: 84px; padding: 10px 7px; font-size: 18px; }
  .book-controls {
    grid-column: 1 / -1;
    grid-template-columns: 1fr;
    justify-items: start;
    gap: 6px;
    border-top: 1px solid var(--app-divider);
    padding-top: 8px;
  }
  .book-metadata { width: 100%; }
  .book-more { margin-left: auto; }
}

@media (prefers-reduced-motion: reduce) {
  .library-book { transition: none; }
}
</style>
