import { test, expect } from '@playwright/test'

test.use({ baseURL: 'http://127.0.0.1:8011' })

test('character graph resolves canonical IDs and excludes locations, props and unresolved temporary people', async ({ page }) => {
  const first = 'novel-graph-char-1', second = 'novel-graph-char-2'
  await page.route('https://**', route => route.abort())
  await page.route('**/api/v1/novels/ai-browser-fixture/knowledge', route => route.fulfill({ json: {
    version: 1, chapters: [], premise_lock: '', facts: [
      { id:'people', subject:first, predicate:'认识', object:second, entity_type:'character', attributes:{ object_type:'character' } },
      { id:'duplicate', subject:'林风', predicate:'认识', object:'秦柔', entity_type:'character', attributes:{ object_type:'character' } },
      { id:'legacy-group', subject:'青云宗', predicate:'逐出', object:'林风', entity_type:'character' },
      { id:'place', subject:first, predicate:'到访过', object:'黑市坊', entity_type:'character', attributes:{ object_type:'location' } },
      { id:'prop', subject:first, predicate:'持有', object:'铜钥匙', entity_type:'character', attributes:{ object_type:'item' } },
      { id:'unknown', subject:'待命名人物（第4章）', predicate:'认识', object:'林风', entity_type:'character', attributes:{ object_type:'character', unresolved_subject_entity:'true' } },
      { id:'raw', subject:'ephemeral-4-deadbeef', predicate:'认识', object:'林风', entity_type:'character' },
    ],
  } }))
  await page.route('**/api/v1/bible/novels/ai-browser-fixture/bible', route => route.fulfill({ json: {
    characters:[{ id:first, name:'林风', relationships:[] }, { id:second, name:'秦柔', relationships:[] }],
    locations:[],
  } }))
  await page.goto('/book/ai-browser-fixture/characters')
  await expect(page.getByRole('img', { name:'关系图表 - 2 个节点，1 个连接', exact:true })).toBeVisible()
  await expect(page.locator('.crg-empty')).toHaveCount(0)
})
