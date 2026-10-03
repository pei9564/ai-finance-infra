import { test, expect, Page } from "@playwright/test";

const job = (id: string, status = "done") => ({
  id,
  topic: id === "a" ? "每週訪談" : "研究筆記",
  status,
  type: "file",
  created_at: "2026-10-04T09:00:00Z",
  language: "zh",
  duration: 83,
  text: `${id} 的逐字稿`,
  progress: null,
});
async function fixture(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("**/jobs", (r) =>
    r.fulfill({
      json: { jobs: [job("a"), job("b", "running")], queue_size: 0 },
    }),
  );
  await page.route("**/job/*", (r) =>
    r.fulfill({
      json: job(new URL(r.request().url()).pathname.split("/").pop()!),
    }),
  );
  await page.route('**/job/*/notes', r=>r.fulfill({json:{enabled:false,notes:{}}}));
  return errors;
}

test("STT jobs search, keyboard tabs and transcript downloads work on mobile", async ({
  page,
}) => {
  const errors = await fixture(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByRole("tab", { name: "貼上連結" }).focus();
  await page.keyboard.press("End");
  await expect(
    page.getByRole("tab", { name: "任務", exact: true }),
  ).toBeFocused();
  await expect(page.locator(".jobrow")).toHaveCount(2);
  await page.getByLabel("任務狀態").selectOption("done");
  await expect(page.locator(".jobrow")).toHaveCount(1);
  await page.getByLabel("搜尋任務").fill("不存在");
  await expect(page.locator("#jobsList")).toContainText("沒有符合條件");
  await page.getByLabel("搜尋任務").fill("訪談");
  await page.getByRole("button", { name: "閱讀逐字稿" }).click();
  await expect(page.getByLabel("逐字稿內容")).toHaveValue("a 的逐字稿");
  await expect(page.getByRole("link", { name: "SRT 字幕" })).toHaveAttribute(
    "href",
    "/job/a/export?format=srt",
  );
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "下載 TXT" }).click();
  const file = await download;
  expect(file.suggestedFilename()).toBe("transcript.txt");
  const stream = await file.createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
  expect(Buffer.concat(chunks).toString()).toBe("a 的逐字稿");
  // Clipboard may be unavailable over a Tailscale HTTP address; fallback selects all text.
  await page.evaluate(() =>
    Object.defineProperty(navigator, "clipboard", {
      value: undefined,
      configurable: true,
    }),
  );
  await page.getByRole("button", { name: "複製全文" }).click();
  await expect(page.locator("#toast")).toContainText("Ctrl+C");
  expect(
    await page
      .locator("#result")
      .evaluate((e: HTMLTextAreaElement) => e.selectionEnd - e.selectionStart),
  ).toBe("a 的逐字稿".length);
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBeTruthy();
  }
  expect(errors).toEqual([]);
});

test("STT stale job responses cannot replace the selected transcript", async ({
  page,
}) => {
  const errors = await fixture(page);
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  await page.route("**/job/a", async (r) => {
    await gate;
    await r.fulfill({ json: job("a") });
  });
  await page.goto("/");
  await page.getByRole("tab", { name: "任務", exact: true }).click();
  await page.getByRole("button", { name: "每週訪談", exact: true }).click();
  await page.getByRole("button", { name: "研究筆記", exact: true }).click();
  await expect(page.locator("#result")).toHaveValue("b 的逐字稿");
  const response = page.waitForResponse("**/job/a");
  release();
  await response;
  await expect(page.locator("#resultTitle")).toHaveText("研究筆記");
  await expect(page.locator("#result")).toHaveValue("b 的逐字稿");
  expect(errors).toEqual([]);
});

test("STT rejects invalid inputs and blocks duplicate submissions while uploading", async ({
  page,
}) => {
  const errors = await fixture(page);
  let calls = 0;
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  await page.route("**/transcribe_file", async (r) => {
    calls++;
    await gate;
    await r.fulfill({ status: 202, json: { job_id: "a" } });
  });
  await page.goto("/");
  await page.getByLabel("YouTube / Podcast 單集網址").fill("bad-link");
  await page.getByRole("button", { name: "開始轉成文字", exact: true }).click();
  await expect(page.locator("#message-url")).toContainText("完整的 http");
  await page.getByRole("tab", { name: "上傳檔案" }).click();
  await page.locator("#file").setInputFiles({
    name: "notes.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("text"),
  });
  await page.getByRole("button", { name: "上傳並轉成文字" }).click();
  await expect(page.locator("#message-file")).toContainText("支援的音訊");
  expect(calls).toBe(0);
  await page.locator("#file").setInputFiles({
    name: "voice.mp3",
    mimeType: "audio/mpeg",
    buffer: Buffer.from("sound"),
  });
  await expect(page.locator("#fileName")).toContainText("voice.mp3");
  await page.getByRole("button", { name: "上傳並轉成文字" }).click();
  await expect(page.locator("#submit-file")).toBeDisabled();
  await expect.poll(() => calls).toBe(1);
  release();
  await expect(page.locator("#result")).toHaveValue("a 的逐字稿");
  await expect(page.locator("#submit-file")).toBeEnabled();
  expect(errors).toEqual([]);
});

test("STT exposes recoverable network errors and confirms deletion", async ({
  page,
}) => {
  const errors = await fixture(page);
  let offline = true,
    deletions = 0;
  await page.route("**/job/a", (r) => {
    if (r.request().method() === "DELETE") {
      deletions++;
      return r.fulfill({ json: { ok: true } });
    }
    return r.fulfill(
      offline
        ? { status: 503, json: { detail: "暫時離線" } }
        : { json: job("a") },
    );
  });
  await page.goto("/");
  await page.getByRole("tab", { name: "任務", exact: true }).click();
  await page.getByRole("button", { name: "每週訪談", exact: true }).click();
  await expect(page.locator("#status")).toContainText("查詢失敗");
  offline = false;
  await page.getByRole("button", { name: "重新查詢進度" }).click();
  await expect(page.locator("#result")).toHaveValue("a 的逐字稿");
  const row = page.locator(".jobrow").filter({ hasText: "每週訪談" });
  page.once("dialog", (d) => d.dismiss());
  await row.getByRole("button", { name: "刪除", exact: true }).click();
  expect(deletions).toBe(0);
  page.once("dialog", (d) => d.accept());
  await row.getByRole("button", { name: "刪除", exact: true }).click();
  await expect.poll(() => deletions).toBe(1);
  await expect(page.locator("#resultContent")).toBeHidden();
  expect(errors).toEqual([]);
});

const timedTranscript = {
  ...job("a"),
  text: "市場需求穩定。\n第二段的市場觀察。\n<script>純文字，不是程式</script>",
  srt: "1\r\n00:00:01,250 --> 00:00:04,500\r\n市場需求穩定。\r\n\r\n2\r\n01:02:03,000 --> 01:02:08,000\r\n第二段的市場觀察。\r\n<script>純文字，不是程式</script>\r\n",
};

test("STT timeline searches multiline SRT safely and copies individual segments", async ({
  page,
}) => {
  const errors = await fixture(page);
  await page.route("**/job/a", (r) => r.fulfill({ json: timedTranscript }));
  await page.goto("/");
  await page.getByRole("tab", { name: "任務", exact: true }).click();
  await page.getByRole("button", { name: "每週訪談", exact: true }).click();
  await expect(page.locator(".segment")).toHaveCount(2);
  await expect(page.locator(".segment-time").first()).toHaveText(
    "00:00:01.250 --> 00:00:04.500",
  );
  await expect(page.locator("#segments script")).toHaveCount(0);
  await page.getByLabel("搜尋逐字稿", { exact: true }).fill("第二段");
  await expect(page.locator(".segment")).toHaveCount(1);
  await expect(page.locator(".segment mark")).toHaveText("第二段");
  await page.evaluate(() =>
    Object.defineProperty(navigator, "clipboard", {
      value: undefined,
      configurable: true,
    }),
  );
  await page.getByRole("button", { name: "複製這段" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByLabel("待複製段落")).toHaveValue(
    "第二段的市場觀察。\n<script>純文字，不是程式</script>",
  );
  await page.keyboard.press("Escape");
  await page.getByLabel("搜尋逐字稿", { exact: true }).fill("不存在");
  await expect(page.locator("#searchSummary")).toHaveText("0 / 2 段符合搜尋");
  await page.getByRole("button", { name: "清除搜尋", exact: true }).click();
  await page.getByRole("button", { name: "純文字", exact: true }).click();
  await page.getByLabel("搜尋逐字稿", { exact: true }).fill("市場");
  await expect(page.locator("#searchSummary")).toHaveText(
    "第 1 / 2 個符合結果",
  );
  await page.getByRole("button", { name: "下一個符合結果" }).click();
  await expect(page.locator("#searchSummary")).toHaveText(
    "第 2 / 2 個符合結果",
  );
  expect(
    await page
      .locator("#result")
      .evaluate((e: HTMLTextAreaElement) =>
        e.value.slice(e.selectionStart, e.selectionEnd),
      ),
  ).toBe("市場");
  await page.getByRole("button", { name: "下一個符合結果" }).click();
  await expect(page.locator("#searchSummary")).toHaveText(
    "第 1 / 2 個符合結果",
  );
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await page.getByRole("button", { name: "時間軸", exact: true }).click();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBeTruthy();
  }
  expect(errors).toEqual([]);
});

test("STT restores selection and filters after reload without storing transcript content", async ({
  page,
}) => {
  const errors = await fixture(page);
  await page.route("**/job/a", (r) => r.fulfill({ json: timedTranscript }));
  await page.goto("/");
  await page.getByRole("tab", { name: "任務", exact: true }).click();
  await page.getByLabel("搜尋任務").fill("訪談");
  await page.getByLabel("任務狀態").selectOption("done");
  await page.getByRole("button", { name: "每週訪談", exact: true }).click();
  await expect(page.locator(".segment")).toHaveCount(2);
  await page.getByRole("button", { name: "純文字", exact: true }).click();
  await page.reload();
  await expect(
    page.getByRole("tab", { name: "任務", exact: true }),
  ).toHaveAttribute("aria-selected", "true");
  await expect(page.getByLabel("搜尋任務")).toHaveValue("訪談");
  await expect(page.getByLabel("任務狀態")).toHaveValue("done");
  await expect(page.locator("#result")).toBeVisible();
  await expect(page.locator("#result")).toHaveValue(timedTranscript.text);
  const state = await page.evaluate(() =>
    localStorage.getItem("voice-notes.workspace.v1"),
  );
  expect(state).not.toContain("市場");
  expect(JSON.parse(state!).job).toBe("a");
  await page.route("**/job/a", (r) =>
    r.fulfill({ status: 404, json: { detail: "job not found" } }),
  );
  await page.reload();
  await expect(page.locator("#status")).toContainText("任務已不存在");
  expect(
    await page.evaluate(
      () => JSON.parse(localStorage.getItem("voice-notes.workspace.v1")!).job,
    ),
  ).toBeNull();
  await page.evaluate(() =>
    localStorage.setItem("voice-notes.workspace.v1", "broken json"),
  );
  await page.reload();
  await expect(page.getByRole("tab", { name: "貼上連結" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  expect(errors).toEqual([]);
});

test("STT upload transport reports bytes, aborts, recovers errors and waits for acceptance", async ({
  page,
}) => {
  const errors = await fixture(page);
  // Browser upload progress cannot be controlled with route fulfillment. A fake transport
  // drives progress/abort/error events; flows.spec.ts covers the actual multipart request.
  await page.addInitScript(() => {
    class Upload {
      upload: any = {};
      status = 0;
      responseText = "";
      onload: any;
      onerror: any;
      onabort: any;
      open() {}
      send() {
        (window as any).testUpload = this;
      }
      abort() {
        this.onabort();
      }
    }
    (window as any).XMLHttpRequest = Upload;
  });
  await page.goto("/");
  await page.getByRole("tab", { name: "上傳檔案" }).click();
  await page
    .locator("#file")
    .setInputFiles({
      name: "audio.mp3",
      mimeType: "audio/mpeg",
      buffer: Buffer.from("audio"),
    });
  await page.getByRole("button", { name: "上傳並轉成文字" }).click();
  await page.evaluate(() =>
    (window as any).testUpload.upload.onprogress({
      lengthComputable: true,
      loaded: 1048576,
      total: 2097152,
    }),
  );
  await expect(page.locator("#uploadStatus")).toContainText(
    "50% · 已傳送 1.0 / 2.0 MB",
  );
  await expect(page.locator("#file")).toBeDisabled();
  await page.getByRole("button", { name: "取消上傳", exact: true }).click();
  await expect(page.locator("#message-file")).toContainText("已停止上傳");
  await expect(page.locator("#submit-file")).toBeEnabled();
  await expect(page.locator("#file")).toBeEnabled();
  await page.getByRole("button", { name: "上傳並轉成文字" }).click();
  await page.evaluate(() => (window as any).testUpload.onerror());
  await expect(page.locator("#message-file")).toContainText("上傳連線中斷");
  await expect(page.locator("#submit-file")).toBeEnabled();
  await page.getByRole("button", { name: "上傳並轉成文字" }).click();
  await page.evaluate(() => (window as any).testUpload.upload.onload());
  await expect(page.locator("#uploadStatus")).toContainText("等待服務建立任務");
  await expect(
    page.getByRole("button", { name: "取消上傳", exact: true }),
  ).toBeDisabled();
  await page.evaluate(() => {
    const x = (window as any).testUpload;
    x.status = 202;
    x.responseText = JSON.stringify({ job_id: "a" });
    x.onload();
  });
  await expect(page.locator("#result")).toHaveValue("a 的逐字稿");
  await expect(page.locator("#uploadStatus")).toContainText("任務已建立");
  expect(errors).toEqual([]);
});

test("STT completion notifications require opt-in and do not repeat on reload", async ({
  page,
}) => {
  const errors = await fixture(page);
  let done = false;
  await page.addInitScript(() => {
    (window as any).permissionRequests = 0;
    (window as any).notices = [];
    class Notice {
      static permission = "granted";
      static async requestPermission() {
        (window as any).permissionRequests++;
        return "granted";
      }
      constructor(title: string) {
        (window as any).notices.push(title);
      }
    }
    (window as any).Notification = Notice;
  });
  await page.clock.install();
  await page.route("**/job/a", (r) =>
    r.fulfill({ json: job("a", done ? "done" : "running") }),
  );
  await page.goto("/");
  expect(await page.evaluate(() => (window as any).permissionRequests)).toBe(0);
  await page.getByText("服務狀態與模型資訊", { exact: true }).click();
  await page.getByRole("button", { name: "開啟完成通知" }).click();
  expect(await page.evaluate(() => (window as any).permissionRequests)).toBe(1);
  await page.getByRole("tab", { name: "任務", exact: true }).click();
  await page.getByRole("button", { name: "每週訪談", exact: true }).click();
  await expect(page.locator("#resultBadge")).toHaveText("轉錄中");
  done = true;
  await page.clock.fastForward(3100);
  await expect(page).toHaveTitle("✓ 轉錄完成 · Voice Notes");
  expect(await page.evaluate(() => (window as any).notices)).toEqual([
    "逐字稿已完成",
  ]);
  await page.reload();
  await expect(page.locator("#result")).toHaveValue("a 的逐字稿");
  expect(await page.evaluate(() => (window as any).notices)).toEqual([]);
  expect(await page.evaluate(() => (window as any).permissionRequests)).toBe(0);
  expect(errors).toEqual([]);
});
