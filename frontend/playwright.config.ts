import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  timeout: 45_000,
  workers: 1,
  retries: 0,
  reporter: 'list',
  webServer: {
    command: `"${process.env.XUHENG_TEST_PYTHON || (process.platform === 'win32' ? '..\\.venv\\Scripts\\python.exe' : '../.venv/bin/python')}" ../scripts/test_ai_browser_server.py --port 8011`,
    url: 'http://127.0.0.1:8011/health',
    reuseExistingServer: false,
    timeout: 60_000,
  },
  use: {
    baseURL: process.env.XUHENG_TEST_URL || 'http://127.0.0.1:8005',
    browserName: 'chromium',
    channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge',
    headless: true,
    viewport: { width: 1440, height: 1000 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
})
