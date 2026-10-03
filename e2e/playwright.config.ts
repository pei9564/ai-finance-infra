import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 45_000,
  expect: { timeout: 20_000 },
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    browserName: 'chromium',
    baseURL: `http://127.0.0.1:${process.env.E2E_STT_PORT || 18798}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  webServer: {
    command: `${process.env.E2E_PYTHON || '.venv/bin/python'} serve.py`,
    url: `http://127.0.0.1:${process.env.E2E_CONTROL_PORT || 18799}/readyz`,
    reuseExistingServer: false,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 10_000 },
    timeout: 120_000,
  },
});
