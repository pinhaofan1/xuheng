import { test, expect } from '@playwright/test'
test.use({ baseURL: 'http://127.0.0.1:8011' })
const book = 'ai-browser-fixture'
const root = `/api/v1/evidence-reviews/${book}`

test('interrupted task can be explicitly retried and its exact result opened', async ({page}) => {
  const errors: string[] = []
  page.on('pageerror', e => errors.push(e.message))
  await page.goto(`/book/${book}/operations`)
  await expect(page.getByRole('heading', {name:'任务与用量',exact:true})).toBeVisible()
  await expect(page.locator('.usage-strip')).toBeVisible()
  await page.screenshot({path:'../.validation/operations-desktop.png',fullPage:true,animations:'disabled'})
  const interrupted = page.locator('.job-row').filter({has:page.locator('.job-status.interrupted')})
  await expect(interrupted).toHaveCount(1)
  await interrupted.getByRole('button',{name:'重新发起',exact:true}).click()
  const submitted = page.waitForResponse(r => r.url().includes('fixture-interrupted/retry') && r.request().method()==='POST')
  await page.getByRole('button',{name:'确认重新发起',exact:true}).click()
  const retry = await (await submitted).json()
  await expect(page.locator('.job-row').filter({has:page.locator('.job-status.completed')}).first()).toBeVisible({timeout:15000})
  await expect.poll(async () => (await (await page.request.get(`${root}/jobs/${retry.id}`)).json()).status).toBe('completed')
  await page.getByRole('button',{name:'刷新记录',exact:true}).click()
  const resultLink = page.locator(`a[href*="run=${retry.id}"]`)
  await expect(resultLink).toBeVisible()
  await resultLink.click()
  await expect(page.locator('.result-meta').filter({hasText:'fixture-test-only'})).toBeVisible()
  await expect(page.getByText('浏览器测试响应，非真实模型结果。',{exact:true})).toBeVisible()
  expect(errors).toEqual([])
})

test('saved zero budget blocks a model request before any charge is recorded', async ({page}) => {
  await page.goto(`/book/${book}/operations`)
  await page.getByRole('button',{name:'设置作品预算',exact:true}).click()
  await page.getByPlaceholder('不限',{exact:true}).nth(0).fill('0')
  await page.getByRole('button',{name:'保存预算',exact:true}).click()
  await expect(page.getByText('预算已保存，下次模型请求前生效',{exact:true})).toBeVisible()
  const before = (await (await page.request.get(`${root}/operations`)).json()).totals.calls
  await page.goto(`/book/${book}/review/2?mode=ai`)
  await page.getByRole('button',{name:'AI 深度审校',exact:true}).click()
  await expect(page.getByText(/本次请求将超过作品请求次数预算/)).toBeVisible({timeout:15000})
  const after = (await (await page.request.get(`${root}/operations`)).json()).totals.calls
  expect(after).toBe(before)
  await page.request.put(`${root}/operations/policy`,{data:{max_calls:null,max_tokens:null,max_cost:null,prices:[]}})
})

test('operations layout works on a narrow screen and budget edits survive background refresh', async ({page}) => {
  await page.setViewportSize({width:390,height:844})
  await page.goto(`/book/${book}/operations`)
  await expect(page.getByRole('heading',{name:'任务与用量',exact:true})).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({path:'../.validation/operations-mobile.png',fullPage:true,animations:'disabled'})
  await page.getByRole('button',{name:'设置作品预算',exact:true}).click()
  await page.getByPlaceholder('不限',{exact:true}).nth(0).fill('123')
  await page.waitForResponse(r => r.url().endsWith('/operations') && r.request().method()==='GET')
  await expect(page.getByPlaceholder('不限',{exact:true}).nth(0)).toHaveValue('123')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({path:'../.validation/operations-budget-mobile.png',fullPage:false,animations:'disabled'})
})

test('unconfirmed generated manuscript can be recovered without replacing saved text', async ({page}) => {
  const before=await(await page.request.get(`/api/v1/novels/${book}/chapters/1`)).json()
  await page.goto(`/book/${book}/operations`)
  await expect(page.getByRole('heading',{name:'托管章节检查点',exact:true})).toBeVisible()
  await expect(page.getByText('生成稿与数据库正文尚未一致，请先取回稿件并确认正文，再恢复托管。',{exact:true})).toBeVisible()
  const downloadPromise=page.waitForEvent('download')
  await page.getByRole('button',{name:'取回生成稿',exact:true}).click()
  const download=await downloadPromise
  expect(download.suggestedFilename()).toBe('chapter-1-recovered.md')
  const {readFile}=await import('node:fs/promises')
  expect(await readFile((await download.path())!,'utf-8')).toContain('浏览器测试生成稿')
  const after=await(await page.request.get(`/api/v1/novels/${book}/chapters/1`)).json()
  expect(after.content).toBe(before.content)
  await page.evaluate(()=>window.scrollTo(0,0))
  await page.screenshot({path:'../.validation/checkpoints-desktop.png',fullPage:true,animations:'disabled'})
})
