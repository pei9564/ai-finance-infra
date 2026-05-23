---
title: "📓 工程決策日誌"
---

# 工程決策日誌(Build Log)

這頁記錄平台**每天真實發生的工程決策與變更**。它跟 ADR 紀錄 是兩個層級:

- **ADR**(重)— 只給*不可逆、高風險*的決策(編排器選型、HA 策略)。一個月可能只有一兩條。
- **Build Log**(輕)— 給其餘所有「我今天動了什麼、為什麼」。這是上面那份私有每日日誌策展後的對外版本。

> [!NOTE]
> 這是策展過的對外紀錄,不是原始日記。原始的每日 scratch 寫在私有空間,每週蒸餾出真正有訊號的幾條到這裡。

## 🔖 標記慣例

`🟢 Done` · `🚧 In progress` · `🧪 Experiment` · `🔥 Incident`(事件另附 postmortem)

---

## 條目格式

每條維持 inverted pyramid — 結論先行,細節在下:

```
### YYYY-MM-DD · <一句話標題> [標記]
- **觸發點:** 什麼狀況逼出這個決策(指標、報錯、瓶頸)。
- **做了什麼:** 具體變更。
- **為什麼:** 取捨;被否決的替代方案。
- **量化影響:** 數字(before → after)。沒量到就標「待量測」。
- **後續:** 衍生的 TODO,或升級成 ADR 的觸發條件。
```

---

## 紀錄

> [!NOTE]
> 下面條目從服務 repo 的 commit 歷史蒸餾而來。「做了什麼」是 commit 事實;標「待量測」的數字請補上實測值 — 那是把這些變成履歷訊號的關鍵。

### 2026-05-21 · K3s 遷移前置:加上 health/ready 探針 + CI 測試門檻 🚧 In progress
- **觸發點:** 準備從 docker-compose 遷往 K3s,但服務沒有 readiness/liveness 探針,K3s 無法做正確的 pod 生命週期管理。
- **做了什麼:** scraper 加 health/ready endpoint、CI 補上跑測試的 stage(`534aece`);同期用 pydantic 鎖外部資料邊界(`c486982`)。
- **為什麼:** 探針是 K3s 排程的前提。先在 docker-compose 階段補齊,遷移時就不用同時改架構又補可觀測性。
- **量化影響:** 待量測(測試覆蓋率、探針回應時間)。
- **後續:** K3s manifest 落地時接上 probe;相關取捨見草擬中的 ADR 紀錄。

### 2026-05-20 · 統一資料流:collect → STT → translate → summarize 🟢 Done
- **觸發點:** 多來源(news / paper / YouTube / podcast)各自有分歧的處理路徑,難維護、加新來源也痛。
- **做了什麼:** 重構成單一線性管線 collect → STT → translate → summarize(`5a1199c`);all-feeds 視圖解鎖跨來源翻譯/摘要(`b91cec1`)。
- **為什麼:** 統一資料流後,「加一個新來源」變成接上管線開頭,而不是改散落各處的邏輯。
- **量化影響:** 待量測(新增一個來源所需改動行數 before → after)。
- **後續:** 這條夠重 → 可升級成一篇「ingestion 管線契約」ADR。

### 2026-05-19 · 用 (source, url) 快取避免重複摘要 🟢 Done
- **觸發點:** rerun 時,已摘要過的事件被重新送進 LLM,白白吃掉 GPU 推論時間。
- **做了什麼:** 以 `(source, url)` 為 key 快取,rerun 跳過已處理事件(`adf9117`);storage 改成 uid 衝突時 upsert(`b6bc844`)。
- **為什麼:** 在單 GPU、共置的資源約束下,避免重複推論直接省 GPU 時間;upsert 讓 pipeline 可安全重跑(冪等)。
- **量化影響:** 待量測(rerun 跳過比例、省下的推論秒數)。
- **後續:** 冪等性是未來 K3s 上 job 重試的前提。

### 2026-05-17 · 把 STT worker 接進 ingestion:YouTube / Podcast 轉錄 🟢 Done
- **觸發點:** 金融資訊大量藏在影音裡(法說會、YouTube、podcast),純文字來源抓不到。
- **做了什麼:** scraper 透過 `stt-ai-worker` 的轉錄,把 YouTube + Podcast 變成可摘要的文字來源(`719c125`);LLM 不可用時 fallback 到原始 transcript(`6b89037`)。
- **為什麼:** STT 是獨立服務(跑在 GPU node),scraper 只消費它的輸出 — 這就是兩個服務解耦、非同步協作的實例。
- **量化影響:** 待量測(音訊來源數、STT real-time factor)。
- **後續:** 跨服務契約該用 pydantic/schema 正式化(已於 `c486982` 起步)。

### 2026-05-16 · 基礎設施落地:單機 docker-compose 共置全棧 🟢 Done
- **觸發點:** 需要把 Postgres / Redis / scraper / stt-worker 一起跑起來,先求能動。
- **做了什麼:** 建 `ai-finance-infra` repo,docker-compose 把四個服務全部放上 `node-a`(`46d5583`);image 指向私有 GHCR(`b4d87f0`)。
- **為什麼:** POC 階段「能動 > 隔離」。但這個共置正是 `node-a` swap 壓力的根源。
- **量化影響:** 待量測(`node-a` 尖峰 RAM / swap)。
- **後續:** 共置壓力是驅動 K3s 遷移做資源隔離的主因 → 見草擬中的 ADR 紀錄。

### 2026-05-15 · 把 LLM 後端抽象在 gateway 介面後(移除 Gemini 直連)🟢 Done
- **觸發點:** 摘要直接綁死外部 Gemini 公有雲 API — 後端不可抽換,且與「零雲洩漏」的架構目標衝突。
- **做了什麼:** 移除 Gemini 直連,改打到一層抽象的 LLM gateway 介面(`38f9545`、`793aea1`);各來源獨立的 prompt/長度(`e70ef14`);後端不可用時降級到原始 transcript(`082e3b6`)。
- **為什麼:** 先把 LLM 後端藏在 gateway 介面後,讓「之後抽換成自架本地模型」變成換 endpoint、而非重寫整條 pipeline;降級確保後端故障時 ingestion 不整個停。
- **量化影響:** 移除一個公有雲 API 的直接依賴。**注意:此步尚未達成零雲洩漏 — gateway 後端目前仍是外部推論服務;真正的零洩漏要等自架本地模型落地**(見 [[adr-005-local-inference-runtime|ADR-005]])。
- **後續:** 自架本地模型遷移計畫草擬中(node-a GPU + 量化 open-weight 模型)。
