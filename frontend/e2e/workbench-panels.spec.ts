import { test, expect } from '@playwright/test'

test.use({ baseURL: 'http://127.0.0.1:8011' })

test('writing support and knowledge panels load with successful data requests', async ({ page }) => {
  const errors: string[] = []
  const failedRequests: string[] = []
  const pending = new Set<object>()
  page.on('request', request => {
    if (request.url().includes('/api/') && !/stream|\/events(?:\?|$)/.test(request.url())) pending.add(request)
  })
  page.on('requestfinished', request => pending.delete(request))
  page.on('requestfailed', request => pending.delete(request))
  page.on('pageerror', error => errors.push(error.message))
  page.on('response', response => {
    if (response.url().startsWith('http://127.0.0.1:8011/') && response.status() >= 400)
      failedRequests.push(`${response.status()} ${new URL(response.url()).pathname}`)
  })
  await page.route('https://**', route => route.abort())
  await page.goto('/book/ai-browser-fixture/workbench')
  await page.getByRole('button', { name: '写作支撑', exact: true }).click()
  for (const [tab, selector] of [['context', '.ctx-panel'], ['foreshadow', '.fsw-panel'], ['story-evolution', '.story-evolution-panel']]) {
    await page.locator(`.settings-tabs .n-tabs-tab[data-name="${tab}"]`).click()
    await expect(page.locator(selector)).toBeVisible()
  }
  await page.getByRole('button', { name: '作品基础', exact: true }).click()
  await page.locator('.settings-tabs .n-tabs-tab[data-name="knowledge"]').click()
  await expect(page.locator('.kp-root')).toBeVisible()
  await expect(page.locator('.panel-load-error')).toHaveCount(0)
  // Allow the independently loaded data panels to complete their initial GETs.
  await expect.poll(() => pending.size).toBe(0)
  expect(errors).toEqual([])
  expect(failedRequests).toEqual([])
})

test('missing lazy chunk offers recovery without reloading unsaved text', async ({ page }) => {
  const chunk = '**/assets/CurrentChapterContextPanel-*.js'
  await page.route('https://**', route => route.abort())
  await page.route(chunk, route => route.fulfill({ status: 404, body: '', contentType: 'application/javascript' }))
  await page.goto('/book/ai-browser-fixture/workbench')
  const editor = page.getByRole('textbox', { name: '章节内容...' })
  await expect(editor).toBeVisible()
  const originalText = await editor.inputValue()
  await editor.fill('尚未保存的正文：面板加载失败时也必须保留。')
  await page.getByRole('button', { name: '写作支撑', exact: true }).click()
  await page.locator('.settings-tabs .n-tabs-tab[data-name="context"]').click()
  await expect(page.locator('.panel-load-error')).toBeVisible()
  await expect(page.getByText('此面板暂时无法加载', { exact: true })).toBeVisible()
  await expect(editor).toHaveValue('尚未保存的正文：面板加载失败时也必须保留。')
  // Restore the untouched fixture text before explicitly choosing to reload.
  await editor.fill(originalText)
  await page.unroute(chunk)
  await page.locator('.panel-load-error').getByRole('button', { name: '刷新页面', exact: true }).click()
  await expect(editor).toBeVisible()
  await page.getByRole('button', { name: '写作支撑', exact: true }).click()
  await page.locator('.settings-tabs .n-tabs-tab[data-name="context"]').click()
  await expect(page.locator('.ctx-panel')).toBeVisible()
  await expect(page.locator('.panel-load-error')).toHaveCount(0)
})
