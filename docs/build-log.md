---
title: "📓 工程決策日誌"
---

# 工程決策日誌（Build Log）

每天實際發生的工程決策跟變更，從各服務 repo 的 commit 整理而來。不可逆、高風險的走 ADR；其餘日常取捨都在這裡。

---

### 2026-05-21 · K3s 遷移前置：health/ready 探針 + CI 測試門檻 🚧 In progress
scraper 加 health/ready endpoint、CI 補上跑測試 stage（`534aece`）；同期用 pydantic 鎖外部資料邊界（`c486982`）。

### 2026-05-20 · 統一資料流：collect → STT → translate → summarize 🟢 Done
重構成單一線性管線 collect → STT → translate → summarize（`5a1199c`）；all-feeds 視圖解鎖跨來源翻譯/摘要（`b91cec1`）。

### 2026-05-19 · 用 (source, url) 快取避免重複摘要 🟢 Done
以 `(source, url)` 為 key 快取，rerun 跳過已處理事件（`adf9117`）；storage 改成 uid 衝突時 upsert（`b6bc844`）。

### 2026-05-17 · STT worker 接進 ingestion：YouTube / Podcast 轉錄 🟢 Done
scraper 透過 `stt-ai-worker` 的轉錄，把 YouTube + Podcast 變成可摘要的文字來源（`719c125`）；LLM 不可用時 fallback 到原始 transcript（`6b89037`）。

### 2026-05-16 · 基礎設施落地：docker-compose 共置全棧 🟢 Done
建 `ai-finance-infra` repo，docker-compose 把四個服務全部放上 `node-a`（`46d5583`）；image 指向私有 GHCR（`b4d87f0`）。

### 2026-05-15 · LLM 後端抽象到 gateway 介面後（移除 Gemini 直連）🟢 Done
移除 Gemini 直連，改打到一層抽象的 LLM gateway 介面（`38f9545`、`793aea1`）；各來源獨立 prompt/長度（`e70ef14`）；後端不可用時降級到原始 transcript（`082e3b6`）。
