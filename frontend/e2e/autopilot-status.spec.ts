import { test, expect } from '@playwright/test'

test.use({ baseURL: 'http://127.0.0.1:8011' })

test('cockpit shows a dead executor and never announces success before Start succeeds', async ({ page }) => {
  let running = true
  let startReceived = false
  let requestedProtection = 0
  let releaseStart: () => void = () => {}
  const startGate = new Promise<void>(resolve => { releaseStart = resolve })
  await page.route('https://**', route => route.abort())
  await page.route('**/api/v1/autopilot/ai-browser-fixture/status', route => route.fulfill({
    json: {
      novel_id: 'ai-browser-fixture', autopilot_status: running ? 'running' : 'stopped',
      current_stage: 'macro_planning', target_chapters: 3, target_words_per_chapter: 500,
      _from_shared_memory: true, daemon_alive: false, daemon_process_running: false,
      auto_approve_mode: false, completed_chapters: 0, max_auto_chapters: 1,
    },
  }))
  await page.route('**/api/v1/autopilot/ai-browser-fixture/stop', async route => {
    running = false
    await route.fulfill({ json: { success: true } })
  })
  await page.route('**/api/v1/autopilot/ai-browser-fixture/start', async route => {
    startReceived = true
    requestedProtection = route.request().postDataJSON().max_auto_chapters
    await startGate
    await route.fulfill({ status: 503, json: { detail: '托管执行进程启动失败，任务未启动。请检查后台日志后重试' } })
  })
  await page.goto('/book/ai-browser-fixture/workbench')
  await page.getByRole('group', { name: '创作模式' }).getByRole('switch').click()
  await expect(page.locator('.ap-stage-tag')).toHaveText('托管执行进程未连接')
  await expect(page.locator('.ap-dot')).toHaveClass(/dot-error/)
  await expect(page.getByText('托管执行进程未连接，当前没有确认任务在执行。', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: '⏹ 停止', exact: true }).click()
  await page.getByRole('button', { name: '🚀 启动全托管', exact: true }).click()
  const protection = page.getByRole('dialog').locator('.n-form-item').filter({ hasText: '保护上限' }).locator('input')
  await protection.fill('1')
  await protection.press('Tab')
  await expect(protection).toHaveValue('1')
  // Advance the intentional Start/Stop cooldown without a real-time sleep.
  // This scenario verifies server acknowledgement rather than click debounce.
  await page.clock.setFixedTime(new Date(Date.now() + 2000))
  await page.getByRole('dialog').getByRole('button', { name: '启动', exact: true }).click()
  await expect.poll(() => startReceived).toBe(true)
  expect(requestedProtection).toBe(1)
  await expect(page.getByText('托管执行进程已就绪，任务已启动', { exact: true })).toHaveCount(0)
  await expect(page.locator('.ap-dot')).not.toHaveClass(/dot-running/)
  releaseStart()
  await expect(page.getByText('托管执行进程启动失败，任务未启动。请检查后台日志后重试', { exact: true })).toBeVisible()
  await expect(page.locator('.ap-dot')).not.toHaveClass(/dot-running/)
})
