import { test as base, expect, APIRequestContext, Page } from '@playwright/test';

const control = `http://127.0.0.1:${process.env.E2E_CONTROL_PORT || 18799}`;
const scraper = `http://127.0.0.1:${process.env.E2E_SCRAPER_PORT || 18797}`;
const transcript = 'Earnings increased by 20 percent. Demand remains strong.';
const translation = '營收成長百分之二十，需求持續強勁。';
const summary = '營收成長 20%，需求持續強勁。';

// Every test starts its own app processes and database state, inside a dedicated DB container.
const test = base.extend<{ pageErrors: string[] }>({
  pageErrors: [async ({ page }, use) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await use(errors);
    expect(errors, 'browser JavaScript errors').toEqual([]);
  }, { auto: true }],
});

test.beforeEach(async ({ request }) => {
  expect((await request.post(`${control}/reset`)).ok()).toBeTruthy();
});

async function upload(page: Page, content: string, topic: string) {
  await page.locator('[data-tab="file"]').click();
  await page.locator('#file').setInputFiles({ name: 'earnings.mp3', mimeType: 'audio/mpeg', buffer: Buffer.from(content) });
  await page.locator('#topic-file').fill(topic);
  const response = page.waitForResponse(r => r.url().endsWith('/transcribe_file') && r.request().method() === 'POST');
  await page.getByRole('button', { name: '上傳並轉成文字' }).click();
  const result = await response;
  expect(result.status()).toBe(202);
  return (await result.json()).job_id as string;
}
async function state(request: APIRequestContext) {
  return (await request.get(`${control}/state`)).json();
}
async function holdWorker(page: Page, request: APIRequestContext) {
  expect((await request.post(`${control}/hold`)).ok()).toBeTruthy();
  const id = await upload(page, 'hold', 'Running upload');
  await expect.poll(async () => (await request.get(`/job/${id}`)).json().then(j => j.status)).toBe('running');
  return id;
}
async function submitUrl(page: Page, title: string) {
  await page.locator('[data-tab="url"]').click();
  await page.locator('#url').fill('https://fixture.invalid/earnings');
  await page.locator('#topic-url').fill(title);
  const response = page.waitForResponse(r => r.url().endsWith('/transcribe_url') && r.request().method() === 'POST');
  await page.getByRole('button', { name: '開始轉成文字', exact: true }).click();
  const result = await response;
  expect(result.status()).toBe(202);
  return (await result.json()).job_id as string;
}

test('input validation and file upload survive browser reload', async ({ page, request }) => {
  await page.goto('/');
  await page.getByRole('button', { name: '開始轉成文字', exact: true }).click();
  await expect(page.locator('#status')).toHaveText('請輸入網址');
  await page.locator('[data-tab="file"]').click();
  await page.getByRole('button', { name: '上傳並轉成文字' }).click();
  await expect(page.locator('#status')).toHaveText('請選擇檔案');
  const id = await upload(page, 'fixture', 'Uploaded earnings');
  await expect(page.locator('#result')).toHaveValue(transcript);
  await expect.poll(async () => (await state(request)).uploads).toBe(0);
  await page.reload();
  await page.locator('[data-tab="jobs"]').click();
  const row = page.locator('.jobrow').filter({ hasText: 'Uploaded earnings' });
  await expect(row.locator('.badge')).toHaveText('已完成');
  await row.getByRole('button', { name: '閱讀逐字稿' }).click();
  await expect(page.locator('#result')).toHaveValue(transcript);
  const persisted = await (await request.get(`/job/${id}`)).json();
  expect(persisted.transcript_id).toBeGreaterThan(0);
  const download = page.waitForEvent('download');
  await row.getByRole('link', { name: 'JSON', exact: true }).click();
  expect((await download).suggestedFilename()).toBe('transcript.json');
  const exported = await (await request.get(`/job/${id}/export`)).json();
  expect(exported.text).toBe(transcript);
  expect(exported.provenance.source).not.toHaveProperty('path');
  expect(exported.provenance.model_profile).toContain('fixture');
});

test('URL and RSS submissions produce transcripts through real APIs', async ({ page }) => {
  await page.goto('/');
  await submitUrl(page, 'URL earnings');
  await expect(page.locator('#result')).toHaveValue(transcript);
  await page.locator('[data-tab="feed"]').click();
  await page.locator('#feed').fill('https://fixture.invalid/feed.xml');
  await page.getByRole('button', { name: '建立 Podcast 轉錄' }).click();
  await expect(page.locator('#feedStatus')).toContainText('已排入 1 集');
  await page.locator('[data-tab="jobs"]').click();
  const row = page.locator('.jobrow').filter({ hasText: 'Fixture episode' });
  await expect(row.locator('.badge')).toHaveText('已完成');
  await row.getByRole('button', { name: '閱讀逐字稿' }).click();
  await expect(page.locator('#result')).toHaveValue(transcript);
});

test('failed upload retains its audio and retries from the jobs UI', async ({ page, request }) => {
  await page.goto('/');
  await upload(page, 'fail-once', 'Retry earnings');
  await expect(page.locator('#status')).toContainText('任務處理失敗');
  expect((await state(request)).uploads).toBe(1);
  await page.locator('[data-tab="jobs"]').click();
  const row = page.locator('.jobrow').filter({ hasText: 'Retry earnings' });
  await row.getByRole('button', { name: '重試', exact: true }).click();
  await expect(row.locator('.badge')).toHaveText('已完成');
  await row.getByRole('button', { name: '閱讀逐字稿' }).click();
  await expect(page.locator('#result')).toHaveValue(transcript);
  await expect.poll(async () => (await state(request)).uploads).toBe(0);
  expect((await state(request)).transcripts).toBe(1);
});

test('cancelling queued jobs keeps the running job and skips cancelled inference', async ({ page, request }) => {
  await page.goto('/');
  await holdWorker(page, request);
  const queued = await submitUrl(page, 'Cancelled earnings');
  await page.locator('[data-tab="jobs"]').click();
  await page.getByText('管理排隊任務', {exact:true}).click();
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: '取消所有排隊中的任務' }).click();
  const row = page.locator('.jobrow').filter({ hasText: 'Cancelled earnings' });
  await expect(row.locator('.badge')).toHaveText('未完成');
  await expect(row.locator('.job-error')).toHaveText('使用者取消');
  expect((await (await request.get(`/job/${queued}`)).json()).status).toBe('error');
  expect((await request.post(`${control}/release`)).ok()).toBeTruthy();
  await expect(page.locator('.jobrow').filter({ hasText: 'Running upload' }).locator('.badge')).toHaveText('已完成');
  expect((await state(request)).transcripts).toBe(1);
});

test('abrupt STT restart restores both running upload and queued URL', async ({ page, request }) => {
  await page.goto('/');
  const running = await holdWorker(page, request);
  const queued = await submitUrl(page, 'Recovered earnings');
  expect((await state(request)).uploads).toBe(1);
  expect((await request.post(`${control}/restart-stt`)).ok()).toBeTruthy();
  await page.reload();
  await page.locator('[data-tab="jobs"]').click();
  for (const title of ['Running upload', 'Recovered earnings']) {
    await expect(page.locator('.jobrow').filter({ hasText: title }).locator('.badge')).toHaveText('已完成');
  }
  for (const id of [running, queued]) {
    expect((await (await request.get(`/job/${id}`)).json()).text).toBe(transcript);
  }
  await expect.poll(async () => (await state(request)).uploads).toBe(0);
  expect((await state(request)).transcripts).toBe(2);
});

test('scraper media flows through STT, translation, summary, bookmark and deletion', async ({ page, request }) => {
  await page.goto(scraper);
  await expect(page.locator('.card-title')).toContainText('Fixture earnings');
  await page.getByRole('button', { name: '🎙️ 轉成文字', exact: true }).click();
  await expect(page.locator('.card-body-preview')).toContainText(transcript);
  await expect(page.locator('.stage-translate')).toHaveText('🌐 需翻譯');
  expect((await state(request)).transcripts).toBe(1);

  await page.locator('button[data-stage="translate"]').click();
  await expect(page.locator('.card-body-preview')).toContainText(translation);
  await expect(page.locator('.card-title')).toContainText('測試財報');
  await page.locator('button[data-stage="summarize"]').click();
  await expect(page.locator('#daily-view .card-summary')).toHaveText(summary);
  const exported = await (await request.get(`${scraper}/api/events/e2e-media/export`)).json();
  expect(exported.processing.original_body).toBe(transcript);
  expect(exported.processing.translation.method).toBe('llm');
  expect(exported.processing.summary.input_truncated).toBe(false);
  expect(exported.processing.stt.job_id).toBeTruthy();
  await page.locator('#daily-view .bookmark-btn').click();
  await expect(page.locator('#daily-view .bookmark-btn')).toHaveClass(/on/);
  await page.reload();
  await expect(page.locator('#daily-view .card-summary')).toHaveText(summary);
  await expect(page.locator('#daily-view .bookmark-btn')).toHaveClass(/on/);
  await page.getByRole('button', { name: '⭐ 我的收藏', exact: true }).click();
  await expect(page.locator('#collection-content')).toContainText('測試財報');
  await expect(page.locator('#collection-content')).toContainText(summary);
  await page.getByRole('button', { name: '← 回到日報', exact: true }).click();
  await page.locator('#daily-view .card-menu > summary').click();
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: '刪除這篇內容', exact: true }).click();
  await expect(page.locator('#feed-content .card')).toHaveCount(0);
  await page.reload();
  await expect(page.locator('#feed-content .card')).toHaveCount(0);
  const bookmarks = await (await request.get(`${scraper}/api/bookmarks/uids`)).json();
  expect(bookmarks).toEqual({ uids: [] });
});


test('duplicate upload reuses completed transcript and exposes diagnostics', async ({ page, request }) => {
  await page.goto('/');
  const first = await upload(page, 'same-content', 'Duplicate earnings');
  await expect(page.locator('#result')).toHaveValue(transcript);
  const second = await upload(page, 'same-content', 'Duplicate earnings');
  expect(second).toBe(first);
  await expect.poll(async () => (await state(request)).uploads).toBe(0);
  expect((await state(request)).transcripts).toBe(1);
  const metrics = await request.get('/metrics');
  expect(metrics.ok()).toBeTruthy();
  expect(await metrics.text()).toContain('http_requests_total');
  expect((await request.get('/diagnostics')).ok()).toBeTruthy();
  expect((await request.post('/transcribe_url', {form: {url:'https://fixture.invalid/x', batch_size:'17'}})).status()).toBe(422);
});

test('transcript AI notes persist independently and export all three formats', async ({page, request})=>{
  await page.goto('/');const id=await upload(page,'notes-fixture','AI notes earnings');
  await expect(page.locator('#result')).toHaveValue(transcript);
  for(const [mode,text] of [['summary',summary],['minutes','討論：營收與需求。'],['actions','未提及明確待辦事項。']]){
    await page.getByLabel('AI 整理方式').selectOption(mode);
    await page.getByRole('button',{name:'產生整理',exact:true}).click();
    await expect(page.locator('#notesText')).toContainText(text);
    const exported=await request.get(`/job/${id}/notes/${mode}/export?format=json`);
    expect(exported.ok()).toBeTruthy();expect((await exported.json()).text).toContain(text);
    const download=page.waitForEvent('download');
    await page.getByRole('link',{name:'下載整理 Markdown'}).click();
    expect((await download).suggestedFilename()).toBe(`${mode}.md`);
  }
  expect((await (await request.get(`/job/${id}`)).json()).text).toBe(transcript);
  expect((await request.post(`${control}/restart-stt`)).ok()).toBeTruthy();
  await page.reload();
  await expect(page.locator('#notesText')).toContainText(summary);
  await expect(page.getByRole('button',{name:'已產生',exact:true})).toBeDisabled();
});

test('media article opens its transcript and links back to Finance Brief',async({page})=>{
  await page.goto(scraper);await page.getByRole('button',{name:'🎙️ 轉成文字',exact:true}).click();
  await expect(page.locator('.card-body-preview')).toContainText(transcript);
  await page.getByRole('link',{name:'開啟對應逐字稿 ↗'}).click();
  await expect(page).toHaveURL(/\?job=/);
  await expect(page.locator('#result')).toHaveValue(transcript);
  await page.getByRole('link',{name:'開啟 Finance Brief ↗'}).click();
  await expect(page.locator('.card-title')).toContainText('Fixture earnings');
  await page.getByRole('link',{name:'開啟 Voice Notes ↗'}).click();
  await expect(page.locator('#result')).toHaveValue(transcript);
});

test('notes cite original segments and retain edited and generated versions',async({page,request})=>{
  await page.goto('/');const id=await upload(page,'versions-fixture','Versioned notes');
  await expect(page.locator('#result')).toHaveValue(transcript);
  await page.getByRole('button',{name:'產生整理',exact:true}).click();
  await expect(page.locator('#notesText .source-link')).toHaveCount(1);
  await page.locator('#notesText .source-link').click();
  await expect(page.locator('.segment')).toHaveClass(/source-highlight/);
  await page.getByRole('button',{name:'編輯我的版本'}).click();
  await page.getByLabel('整理修正版').fill('我的財報重點 [S1]');
  await page.getByRole('button',{name:'儲存修正版'}).click();
  await expect(page.locator('#notesText')).toContainText('我的財報重點');
  await page.reload();await expect(page.locator('#notesText')).toContainText('我的財報重點');
  const markdown=await request.get(`/job/${id}/notes/summary/export`);expect(await markdown.text()).toContain('我的財報重點');
  await page.locator('#noteInstruction').fill('fixture-fail-note');
  page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'重新產生',exact:true}).click();
  await expect(page.locator('#notesStatus')).toContainText('測試重新產生失敗');
  await expect(page.locator('#notesText')).toContainText('我的財報重點');
  await page.locator('#noteInstruction').fill('只整理投資相關內容');
  page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'重新產生',exact:true}).click();
  await expect(page.locator('#notesStatus')).toContainText('fixture-notes');
  await expect(page.locator('#notesText')).toContainText(summary);
  await page.getByLabel('查看版本').selectOption('version:0');
  await expect(page.locator('#notesText')).toContainText('我的財報重點');
  const previous=await request.get(`/job/${id}/notes/summary/export?version=version:0`);expect(await previous.text()).toContain('我的財報重點');
  await page.getByLabel('查看版本').selectOption('original');await expect(page.locator('#notesText')).toContainText(summary);
  expect((await (await request.get(`/job/${id}`)).json()).text).toBe(transcript);
});

test('historical library searches transcript text and persists names tags and favorites',async({page,request})=>{
  await page.goto('/');const id=await upload(page,'library-fixture','Initial title');
  await expect(page.locator('#result')).toHaveValue(transcript);
  await page.getByRole('tab',{name:'任務',exact:true}).click();
  const row=page.locator('.jobrow').filter({hasText:'Initial title'});
  await row.getByRole('button',{name:'收藏任務',exact:true}).click();
  await row.getByRole('button',{name:'名稱與標籤',exact:true}).click();
  await page.getByLabel('任務名稱').fill('我的財報資料庫');await page.getByLabel('標籤（逗號分隔）').fill('財報, 研究');
  await page.getByRole('button',{name:'儲存任務資訊',exact:true}).click();
  expect((await request.post(`${control}/age-job/${id}`)).ok()).toBeTruthy();
  await page.getByLabel('任務時間範圍').selectOption('all');
  await page.getByLabel('搜尋任務').fill('Demand remains strong');
  await page.getByLabel('只看收藏', {exact:true}).check();await page.getByLabel('標籤篩選', {exact:true}).fill('研究');
  await expect(page.locator('.jobrow')).toHaveCount(1);await expect(page.locator('.job-title')).toHaveText('我的財報資料庫');
  expect((await request.post(`${control}/restart-stt`)).ok()).toBeTruthy();await page.reload();
  await expect(page.locator('.jobrow')).toHaveCount(1);await expect(page.locator('.jobrow')).toContainText('#財報 #研究');
  expect((await (await request.get(`/job/${id}`)).json()).favorite).toBe(true);
  await page.getByLabel('標籤篩選', {exact:true}).fill('不存在');await expect(page.locator('.jobrow')).toHaveCount(0);
});

test('news reading later and hidden states persist across reloads',async({page,request})=>{
  await page.goto(scraper);
  await page.getByRole('button',{name:'稍後閱讀',exact:true}).click();
  await expect(page.getByRole('button',{name:'已加入稍後閱讀',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'標為已讀',exact:true}).click();
  await expect(page.getByRole('button',{name:'已讀 · 標為未讀',exact:true})).toBeVisible();
  await page.reload();await expect(page.getByRole('button',{name:'已加入稍後閱讀',exact:true})).toBeVisible();
  await page.getByLabel('閱讀篩選', {exact:true}).selectOption('unread');await expect(page.locator('#feed-content .card')).toHaveCount(0);
  await page.getByLabel('閱讀篩選', {exact:true}).selectOption('later');await expect(page.locator('#feed-content .card')).toHaveCount(1);
  await page.getByRole('button',{name:'隱藏內容',exact:true}).click();await expect(page.locator('#feed-content .card')).toHaveCount(0);
  await page.getByLabel('閱讀篩選', {exact:true}).selectOption('hidden');await expect(page.locator('#feed-content .card')).toHaveCount(1);
  await page.reload();await expect(page.getByLabel('閱讀篩選', {exact:true})).toHaveValue('hidden');
  await page.getByRole('button',{name:'取消隱藏',exact:true}).click();
  await expect(page.locator('#feed-content .card')).toHaveCount(0);
  const states=await (await request.get(`${scraper}/api/reading`)).json();expect(states['e2e-media']).toEqual({read:true,later:true,hidden:false});
});
