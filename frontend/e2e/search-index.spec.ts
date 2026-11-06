import { test, expect } from '@playwright/test'
import { randomUUID } from 'node:crypto'

test('workbench builds a local index and retrieves an exact preceding citation', async ({ page, request }) => {
  const novel = `xh-index-${randomUUID()}`
  await page.route('https://**', route => route.abort())
  const root = `/api/v1/novels/${novel}`
  try {
    const created = await request.post('/api/v1/novels/', { data: {
      novel_id: novel, title: '潮汐索引 · 浏览器验证', author: 'Xuheng E2E', target_chapters: 2,
    } })
    expect(created.status()).toBe(201)
    await expect.poll(async () => (await request.get(root)).status()).toBe(200)
    for (const [number, content] of [[1, '林澈发现了失落的紫铜灯芯。'], [2, '苏禾回到港口。']] as const) {
      const chapter = await request.post(`${root}/chapters`, { data: {
        chapter_id: `${novel}-${number}`, number, title: `第${number}章`, content,
      } })
      expect(chapter.status()).toBe(201)
      await expect.poll(async () => (await request.get(`${root}/chapters/${number}`)).status()).toBe(200)
    }
    await page.goto(`/book/${novel}/review/2`)
    await page.getByRole('tab', { name: '历史检索', exact: true }).click({ timeout: 8000 })
    await page.getByRole('button', { name: '构建索引' }).click({ timeout: 8000 })
    await expect(page.getByText('2/2 章 · 已完成')).toBeVisible()
    await expect(page.getByRole('button', { name: '构建向量索引', exact: true })).toBeVisible()
    await expect(page.getByText(/构建时会向已配置的嵌入服务发送正文片段/)).toBeVisible()
    await page.getByRole('textbox', { name: '历史检索内容' }).fill('紫铜灯芯', { timeout: 8000 })
    await page.getByRole('button', { name: '检索历史正文' }).click({ timeout: 8000 })
    await expect(page.locator('.history-pane blockquote')).toContainText('林澈发现了失落的紫铜灯芯。')
    await page.setViewportSize({ width: 390, height: 844 })
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
    await page.screenshot({ path: '../.validation/search-index-mobile.png' })
  } finally {
    await request.delete(root)
  }
})
