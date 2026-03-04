import { defineConfig, devices } from '@playwright/test'

const baseURL = process.env.QA_BASE_URL || 'https://example.invalid'
const slowMo = Number.parseInt(process.env.QA_SLOW_MO_MS || '200', 10)
const headless = process.env.PW_HEADLESS === '1'

export default defineConfig({
  testDir: './tests',
  timeout: 10 * 60 * 1000,
  expect: { timeout: 30 * 1000 },
  retries: 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL,
    headless,
    launchOptions: {
      slowMo: Number.isFinite(slowMo) ? slowMo : 200
    },
    trace: 'retain-on-failure',
    video: 'retain-on-failure'
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] }
    }
  ]
})
