import { test, expect } from '@playwright/test'
import { randomUUID } from 'node:crypto'
import { mkdir } from 'node:fs/promises'

test('review evidence, feedback persistence, version expiry and mobile layout', async ({ page, request }) => {
  const novel = `xh-e2e-${randomUUID()}`
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  // External font CDNs should not affect a local functional test.
  await page.route('https://**', route => route.abort())
  const content = '🌊潮水漫过石阶。\n\n林烬释放火球，沈澜施展水系法术。\n\n沈澜释放火球，照亮了远处的船帆。\n\n沈澜开枪，惊起了芦苇里的水鸟。\n\n如果林烬施展水系法术，也许能让船停下来。'
  try {
    const created = await request.post('/api/v1/novels/', { data: {
      novel_id: novel, title: '潮声里的枪响 · 浏览器验证', author: 'Xuheng E2E', target_chapters: 2,
    } })
    expect(created.status(), await created.text()).toBe(201)
    await expect.poll(async () => (await request.get(`/api/v1/novels/${novel}`)).status()).toBe(200)
    const chapter = await request.post(`/api/v1/novels/${novel}/chapters`, { data: {
      chapter_id: `${novel}-1`, number: 1, title: '潮声里的枪响', content,
    } })
    expect(chapter.status(), await chapter.text()).toBe(201)
    await expect.poll(async () => (await request.get(`/api/v1/novels/${novel}/chapters/1`)).status()).toBe(200)
    const configured = await request.put(`/api/v1/evidence-reviews/${novel}/rules`, { data: {
      revision: '', profiles: [
        { id: 'lin', name: '林烬', aliases: ['阿烬'], allowed_magic: ['火系'], forbidden_actions: [] },
        { id: 'shen', name: '沈澜', aliases: ['阿澜'], allowed_magic: ['水系'], forbidden_actions: ['use_gun'] },
      ],
    } })
    expect(configured.status(), await configured.text()).toBe(200)
    await page.goto(`/book/${novel}/review/1`)
    await expect(page.getByRole('heading', { name: '潮声里的枪响', exact: true })).toBeVisible()
    await page.getByRole('button', { name: '审校已保存正文' }).click()
    await expect(page.locator('.finding')).toHaveCount(3)
    await expect(page.getByText('2 项设定偏离 · 1 项待确认主体')).toBeVisible()
    await page.locator('.finding').filter({ hasText: '沈澜释放火球' }).click()
    await expect(page.locator('mark')).toHaveText('沈澜释放火球')
    await expect(page.locator('.source-card blockquote')).toContainText('沈澜仅能使用水系')
    await page.getByRole('button', { name: '忽略此项' }).click()
    await expect(page.locator('.finding.selected .status')).toHaveText('已忽略')
    await page.reload()
    await expect(page.locator('.finding').filter({ hasText: '沈澜释放火球' }).locator('.status')).toHaveText('已忽略')
    await page.locator('.finding').filter({ hasText: '沈澜释放火球' }).click()
    await page.getByRole('button', { name: '撤销处理' }).click()
    await expect(page.locator('.finding.selected .status')).toHaveText('待处理')
    await mkdir('../project_docs/screenshots', { recursive: true })
    await page.screenshot({ path: '../project_docs/screenshots/review-desktop.png', fullPage: true })
    await page.setViewportSize({ width: 390, height: 844 })
    await expect(page.getByRole('heading', { name: '章节原文', exact: true })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
    await page.screenshot({ path: '../project_docs/screenshots/review-mobile.png', fullPage: true })
    await page.setViewportSize({ width: 1440, height: 1000 })
    await page.getByRole('button', { name: '审校设置', exact: true }).click()
    await expect(page.getByRole('heading', { name: '人物设定', exact: true })).toBeVisible()
    await page.locator('.profile input').first().fill('林烬甲')
    await expect(page.getByRole('button', { name: '审校已保存正文' })).toBeDisabled()
    await page.getByRole('button', { name: '保存人物设定' }).click()
    await expect(page.getByText('人物设定已保存', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: '完成设置', exact: true }).click()
    await expect(page.getByText('这份审校记录已过期', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: '确认问题' })).toBeDisabled()
    await page.getByRole('button', { name: '审校已保存正文' }).click()
    await expect(page.getByText('这份审校记录已过期', { exact: true })).toHaveCount(0)
    expect(errors).toEqual([])
  } finally {
    // Only remove the unique fixture created by this test.
    await request.delete(`/api/v1/novels/${novel}`)
  }
})
