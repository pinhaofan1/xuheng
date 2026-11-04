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
const color=(index:number)=>['#52627f','#477b78','#887357','#746586'][index%4]
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
  <article v-for="(book,index) in books" :key="book.slug" class="library-book">
   <div class="book-cover" :style="{'--cover-color':color(index)}" aria-hidden="true"><span class="cover-kicker">叙衡 · 创作手稿</span><strong>{{ book.title }}</strong><span class="cover-rule"/><span class="cover-genre">{{book.genre || '长篇叙事'}}</span><AppIcon name="book" :size="28"/></div>
   <div class="book-information"><div class="book-card-top"><span class="book-stage"><i/>{{book.stage_label || '创作中'}}</span><n-dropdown trigger="click" :options="[{label:'删除作品',key:'delete'}]" @select="$emit('delete',book.slug)"><button class="book-more" :disabled="busy" :aria-label="`管理《${book.title}》`"><AppIcon name="more"/></button></n-dropdown></div><h3><button @click="$emit('open',book.slug)">{{book.title}}</button></h3><p class="book-premise">{{cleanPremise(book.premise)}}</p><p class="book-progress"><span>{{book.chapter_count || 0}}<template v-if="book.target_chapters"> / {{book.target_chapters}}</template> 章</span><span>{{(book.word_count || 0).toLocaleString('zh-CN')}} 字</span></p><div class="book-card-actions"><button class="continue-writing" :disabled="busy" @click="$emit('open',book.slug)">继续创作<AppIcon name="arrow" :size="15"/></button><button class="open-review" :disabled="busy || !!opening" @click="reviewBook(book)"><AppIcon name="review" :size="15"/>{{opening===book.slug?'加载中…':'审校'}}</button></div></div>
  </article>
 </div>
</template>
<style scoped>
.book-library{display:grid;
  grid-template-columns:repeat(auto-fill,minmax(410px,1fr));
  gap:22px}
.library-book{display:flex;
  gap:22px;
  border:1px solid var(--app-border);
  border-radius:10px;
  background:var(--app-surface);
  padding:22px;
  min-width:0;
  transition:border-color .16s,box-shadow .16s}
.library-book:hover{border-color:var(--app-border-strong);
  box-shadow:0 6px 22px #26355608}
.book-cover{flex-shrink:0;
  width:118px;
  min-height:180px;
  background:var(--cover-color);
  color:#fff;
  position:relative;
  border-radius:3px 7px 7px 3px;
  padding:17px 15px 17px 20px;
  display:flex;
  flex-direction:column;
  align-items:flex-start;
  box-shadow:inset 5px 0 #00000012,2px 3px 6px #0000000c}
.cover-kicker{font-size:8px;
  letter-spacing:.1em;
  opacity:.7}
.book-cover strong{font:500 17px/1.6 'STSong','SimSun',serif;
  margin:15px 0 12px;
  display:-webkit-box;
  -webkit-line-clamp:3;
  -webkit-box-orient:vertical;
  overflow:hidden;
  letter-spacing:.05em}
.cover-rule{width:20px;
  height:1px;
  background:#ffffff65;
  margin-bottom:10px}
.cover-genre{font-size:9px;
  opacity:.75}
.book-cover svg{margin-top:auto;
  padding-top:14px;
  box-sizing:content-box;
  opacity:.55}
.book-information{flex:1;
  min-width:0;
  display:flex;
  flex-direction:column}
.book-card-top{display:flex;
  align-items:center;
  justify-content:space-between;
  height:23px}
.book-stage{font-size:10px;
  color:var(--app-text-muted);
  display:flex;
  align-items:center;
  gap:6px}
.book-stage i{height:5px;
  width:5px;
  border-radius:50%;
  background:#53958b}
.book-more{border:0;
  background:none;
  color:var(--app-text-muted);
  cursor:pointer;
  padding:5px}
.book-information h3{margin:10px 0 8px}
.book-information h3 button{padding:0;
  border:0;
  background:none;
  color:var(--app-text-primary);
  font:600 16px/1.6 var(--font-sans);
  text-align:left;
  cursor:pointer}
.book-premise{font-size:11px;
  line-height:1.9;
  color:var(--app-text-muted);
  display:-webkit-box;
  -webkit-line-clamp:2;
  -webkit-box-orient:vertical;
  overflow:hidden;
  margin:0 0 12px;
  min-height:40px}
.book-progress{display:flex;
  gap:14px;
  color:var(--app-text-muted);
  font-size:10px;
  margin:auto 0 15px;
  font-variant-numeric:tabular-nums}
.book-card-actions{display:flex;
  align-items:center;
  gap:8px;
  border-top:1px solid var(--app-divider);
  padding-top:12px}
.continue-writing,.open-review{display:flex;
  align-items:center;
  justify-content:center;
  gap:6px;
  border:0;
  background:none;
  font:500 11px var(--font-sans);
  color:var(--color-brand);
  cursor:pointer;
  padding:5px 0}
.continue-writing{margin-right:auto}
.open-review{color:var(--app-text-muted)}
button:focus-visible{outline:2px solid var(--color-brand);
  outline-offset:3px}
button:disabled{opacity:.5;
  cursor:default}

@media(max-width:1100px){.book-library{grid-template-columns:1fr}
.library-book{max-width:720px}
}
@media(max-width:500px){.library-book{padding:16px;
  gap:17px}
.book-cover{width:94px;
  min-height:176px;
  padding:14px}
.book-cover strong{font-size:15px}
.book-information h3 button{font-size:14px}
.book-progress{gap:8px}
.book-premise{-webkit-line-clamp:3;
  font-size:10px}
.cover-kicker{font-size:7px}
}

@media(prefers-reduced-motion:reduce){.library-book{transition:none}
}


</style>
