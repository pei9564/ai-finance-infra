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
  await page
    .locator("#file")
    .setInputFiles({
      name: "notes.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("text"),
    });
  await page.getByRole("button", { name: "上傳並轉成文字" }).click();
  await expect(page.locator("#message-file")).toContainText("支援的音訊");
  expect(calls).toBe(0);
  await page
    .locator("#file")
    .setInputFiles({
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
