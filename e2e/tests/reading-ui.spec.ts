import { test, expect, Page } from '@playwright/test';

const scraper = `http://127.0.0.1:${process.env.E2E_SCRAPER_PORT || 18797}`;
const rows = [
  {uid:'read-one',source:'finance',title:'市場觀察：企業財報',body:'財報內容',body_lang:'zh',ai_summary:'營收與現金流值得持續觀察。',feed_name:'財經新聞'},
  {uid:'read-two',source:'news',title:'研究工具與人工智慧',body:'AI research tools',body_lang:'en',ai_summary:'',feed_name:'科技新聞'},
  {uid:'read-three',source:'youtube_fin',title:'長期投資訪談',body:'',body_lang:null,ai_summary:'',feed_name:'投資筆記'},
].map(e=>({...e,url:'https://example.com/article',created_at:'2026-10-04T00:00:00+08:00',tags:[],processing:{}}));

async function fixture(page: Page, options: {empty?: boolean, unavailable?: boolean} = {}) {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  let submitted = 0;
  let collected = false;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/reading') return route.fulfill({json:{}});
    if (path === '/api/feeds') {
      if (options.unavailable) return route.fulfill({status:503,json:{detail:'offline'}});
      return route.fulfill({json:{feeds:options.empty ? [] : rows.map(e=>({source:e.source,feed_name:e.feed_name,count:1,missing_body:0,needs_translate:0,needs_summary:0}))}});
    }
    if (path === '/api/feeds/events') return route.fulfill({json:{source:null,feed_name:null,groups:options.empty?[]:[{date:'2026-10-04',events:rows,missing:2,needs_translate:1,needs_summary:0}]}});
    if (path === '/api/bookmarks/uids') return route.fulfill({json:{uids:[]}});
    if (path === '/api/collections') return route.fulfill({json:{collections:[{key:'bookmarks',type:'bookmarks',label:'我的收藏',months:[]}]}});
    if (path === '/api/status') return route.fulfill({json:{running:collected,mode:'full'}});
    if (path === '/api/run') {submitted++;collected=true;return route.fulfill({status:202,json:{started:true}});}
    return route.abort(); // Mock scenarios never mutate the application database.
  });
  return {errors, submissions:()=>submitted};
}

test('reading list searches and filters with a recoverable empty state', async ({page}) => {
  const state = await fixture(page);
  await page.goto(scraper);
  await expect(page.locator('#feed-content .card')).toHaveCount(3);
  await page.getByRole('button',{name:'待處理',exact:true}).click();
  await expect(page.locator('#feed-content .card')).toHaveCount(2);
  await expect(page.locator('.stage-btn')).toHaveCount(0); // Filtered lists must not imply a narrower batch mutation.
  await page.getByLabel('搜尋目前來源').fill('投資');
  await expect(page.locator('#feed-content .card')).toHaveCount(1);
  await expect(page.locator('.card-title')).toContainText('長期投資');
  await page.getByLabel('搜尋目前來源').fill('找不到的關鍵字');
  await expect(page.getByText('沒有符合條件的內容')).toBeVisible();
  await page.getByRole('button',{name:'清除篩選',exact:true}).click();
  await expect(page.locator('#feed-content .card')).toHaveCount(3);
  await page.getByRole('button',{name:'已有摘要',exact:true}).click();
  await expect(page.locator('#feed-content .card')).toHaveCount(1);
  await page.locator('.card-menu > summary').click();
  await expect(page.getByRole('link',{name:'下載 Markdown'})).toHaveAttribute('href',/read-one\/export\?format=markdown/);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('link',{name:'下載 Markdown'})).not.toBeVisible();
  expect(state.errors).toEqual([]);
});

test('first visit offers collection guidance and prevents duplicate submissions', async ({page}) => {
  const state = await fixture(page,{empty:true});
  await page.goto(scraper);
  await expect(page.getByText('建立你的第一份資訊日報')).toBeVisible();
  await page.getByRole('button',{name:'＋ 蒐集今天的內容',exact:true}).click();
  await expect(page.locator('#run-today-btn')).toBeDisabled();
  await expect(page.getByRole('button',{name:'＋ 蒐集今天的內容',exact:true})).toBeDisabled();
  expect(state.submissions()).toBe(1);
  expect(state.errors).toEqual([]);
});

test('mobile navigation supports keyboard focus and has no horizontal overflow', async ({page}) => {
  const state = await fixture(page);
  await page.setViewportSize({width:390,height:844});
  await page.goto(scraper);
  await expect(page.locator('#feed-content .card')).toHaveCount(3);
  await page.getByRole('button',{name:'開啟側欄',exact:true}).click();
  await expect(page.getByRole('button',{name:'關閉側欄',exact:true})).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button',{name:'開啟側欄',exact:true})).toBeFocused();
  await expect(page.getByRole('button',{name:'開啟側欄',exact:true})).toHaveAttribute('aria-expanded','false');
  for (const width of [390,320]) {
    await page.setViewportSize({width,height:844});
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBeTruthy();
  }
  expect(state.errors).toEqual([]);
});

test('load failure is distinct from an empty library and can be retried', async ({page}) => {
  const options = {unavailable:true,empty:true};
  const state = await fixture(page,options);
  await page.goto(scraper);
  await expect(page.getByText('暫時無法載入內容')).toBeVisible();
  await expect(page.getByText('建立你的第一份資訊日報')).not.toBeVisible();
  options.unavailable=false;
  await page.getByRole('button',{name:'重新載入',exact:true}).click();
  await expect(page.getByText('建立你的第一份資訊日報')).toBeVisible();
  expect(state.errors).toEqual([]);
});

test('reading position and filters are restored after reload',async({page})=>{
  const state=await fixture(page);
  const events=Array.from({length:30},(_,i)=>({...rows[0],uid:'position-'+i,title:'財報觀察 '+i,ai_summary:'閱讀內容。'.repeat(45)}));
  await page.route('**/api/feeds/events?*',r=>r.fulfill({json:{source:null,feed_name:null,groups:[{date:'2026-10-04',events,missing:0,needs_translate:0,needs_summary:0}]}}));
  await page.goto(scraper);await expect(page.locator('#feed-content .card')).toHaveCount(30);
  await page.getByLabel('搜尋目前來源').fill('財報');await page.getByLabel('閱讀篩選',{exact:true}).selectOption('unread');
  const card=page.locator('[data-reading-uid="position-10"]');
  await card.evaluate(e=>e.scrollIntoView({block:'start'}));
  await expect.poll(()=>page.evaluate(()=>JSON.parse(localStorage.getItem('finance.reading.v1')||'{}').uid)).toBe('position-10');
  await page.reload();await expect(page.getByLabel('搜尋目前來源')).toHaveValue('財報');
  await expect(page.getByLabel('閱讀篩選',{exact:true})).toHaveValue('unread');await expect(card).toBeInViewport();
  expect(state.errors).toEqual([]);
});
