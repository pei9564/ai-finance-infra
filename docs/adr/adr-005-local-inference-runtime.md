---
title: "📜 ADR-005:本地推論的 serving runtime 與模型選型"
---

> **TIP · 互動版 ↗**
> 本決策的可動手互動版：[Serving Runtime 選型 — VRAM 模擬器](demos/serving-runtime-decision.html) — 拖動 VRAM 滑桿、切換「Whisper 是否共卡」，看 vLLM / Ollama / llama.cpp 哪個裝得下、推薦如何翻轉。

# ADR-005:本地推論的 serving runtime 與模型選型

- **Status:** 🟡 Proposed（runtime 已可定；最終模型 pick 待實跑驗證品質與 VRAM）
- **Date:** 2026-05-21
- **Decider:** Pei

## 1. 背景與面臨挑戰

ADR-004（內部草稿）決定把 LLM 推論搬到自架本地模型。這篇接著回答：**用哪套 serving stack**。

硬約束（來自[首頁的硬體基線](./)）：

- `node-a` 只有 **~4 GB VRAM**，且 **Whisper STT 已在佔用同一張卡** — 本地 LLM 必須跟 STT 搶 GPU。
- 負載特性：**單人 home lab、批次逐則摘要、非即時** — 吞吐量不是首要，能在約束內跑得起來、且接得上 [05-15 建立的 gateway 介面](?file=build-log.md) 才是。

## 2. 評估候選方案

### Runtime

#### Option A:vLLM

- **Pros:** 高吞吐（PagedAttention）、production-grade、OpenAI-compatible API。
- **Cons:** VRAM baseline overhead 高（預設 `gpu_memory_utilization=0.9`）；**幾乎不做 CPU offload** — 4 GB 放不下就是放不下；與 STT 共卡極脆弱。是為有專屬 GPU 的高並發服務設計的，不是這個場景。

#### Option B:Ollama

- **Pros:** 一行起服務；GGUF + **GPU/CPU 混合 offload**（VRAM 不夠的層丟 CPU），4 GB 也跑得動；內建模型管理；OpenAI-compatible endpoint → 接既有 gateway 成本最低。
- **Cons:** 吞吐不如 vLLM；並發/批次控制較陽春。

#### Option C:裸 llama.cpp

- **Pros:** 最省、offload 層數可最細調。
- **Cons:** 要自己包 HTTP server / API / 模型管理 — 維運成本高，等於重做 Ollama 已經給的東西。

### 模型（short-list,Q4 量化下 ~2 GB）

| 模型 | 傾向理由 |
|---|---|
| **Qwen2.5-3B-Instruct** | 中文能力強，適合**繁中金融摘要**；傾向首選 |
| Llama-3.2-3B-Instruct | 英文強，中文較弱 |
| Gemma-2-2B | 更省 VRAM，品質天花板較低 |

## 3. 最終決策與核心理據

選擇 **Runtime = Option B（Ollama）**;**模型暫定 Qwen2.5-3B-Instruct Q4_K_M**，最終 pick 待實跑驗證。

> **IMPORTANT**
> **技術理由：** 4 GB VRAM 且與 STT 共卡這條物理約束，直接淘汰 vLLM（無 CPU offload）。Ollama 跟裸 llama.cpp 之間，Ollama 把 llama.cpp 包成 OpenAI-compatible API + 模型管理，**接上既有 gateway 介面只是改個 endpoint**，裸 llama.cpp 等於要自己重做這層。吞吐劣勢在「單人、批次、非即時摘要」的負載下不是瓶頸。模型選 Qwen 是因為下游是繁中金融內容，中文能力 > 英文 benchmark。

## 4. 後續影響

- **Positive:** 本地推論在 4 GB 上實際可行；接 gateway 成本最低；CPU offload 提供 VRAM 不足時的彈性。
- **Negative/Risk:** 與 STT 的 GPU 競爭仍在 → 用 **on-demand 啟動（不 ingestion 時 scale 到 0，把 VRAM 還給 Whisper）** + 未來 K3s 拆節點（[ADR-001](?file=adr/adr-001-k3s-selection.md)）緩解；Q4 量化會掉一些品質。
- **量化（實跑後量，翻 🟢 的條件）:** tokens/s = `[待量測]`；模型 VRAM 佔用 = `[待量測]`；與 Whisper 並行時的降速 = `[待量測]`;Qwen-3B-Q4 摘要品質相對原後端 = `[待量測]`。
- **連動決策：** 為什麼要本地見 ADR-004（內部草稿）；GPU 競爭的根本解見 [ADR-001](?file=adr/adr-001-k3s-selection.md)。
