---
title: "📜 ADR-001:容器編排器選型"
---

> **TIP · 互動版 ↗**
> 本決策的可動手互動版：[四節點異質 home-lab 拓樸](demos/four-node-topology.html) — 點任一節點看角色分派理據，切換「現況 docker-compose ↔ 目標 K3s」看 workload 如何重新落位。

# ADR-001:容器編排器選型

- **Status:** 🟡 Proposed（待 K3s 實際部署、生產驗證後翻 🟢 Accepted）
- **Date:** 2026-05-17
- **Decider:** Pei

## 1. 背景與面臨挑戰

平台目前是 3 套獨立的 `docker-compose` 堆疊，分別跑在 `node-a`、`node-b`、`node-c` 上（見[首頁的硬體基線](./)）。三個痛點逼我們轉去正式的編排器：

1. **沒有 node affinity。** Whisper worker 必須跑在 `node-a` 上唯一可用的 Ampere GPU；目前靠「人工把 docker-compose 檔丟對 host」維持。
2. **沒有資源隔離。** `node-a` 同時跑 Whisper + Postgres + Redis 在 16 GB 上 → 看得見的 swap 壓力，直接拖慢 inference。
3. **沒有跨節點排程。** Container 掛了不會自動 reschedule 到其他 host；唯一的復原路徑是手動 SSH + restart。

候選編排器要能跑在異質 laptop 上、支援 GPU 排程、活在主流 ML/MLOps 生態系裡。

## 2. 評估候選方案

### Option A:Vanilla Kubernetes(`kubeadm`)

- **Pros:** 業界標準，生態系最豐富（NVIDIA GPU Operator、Argo、Strimzi 等）。
- **Cons:** Control-plane 基線 ≥ 2 GB RAM；多個 binary（etcd、kube-apiserver、scheduler、controller-manager、kubelet）要維護。在一台還要扛 Postgres + pgvector 的 30 GB control-plane node 上，這個開銷太奢侈。

### Option B:Docker Swarm

- **Pros:** 上手成本極低 — 直接 `docker stack deploy`。
- **Cons:** 已進入 maintenance 模式，沒有積極開發。NVIDIA GPU 支援是二等公民。MLOps 生態系（KServe、Kubeflow、Argo Workflows）清一色 Kubernetes-first；選 Swarm 等於把平台鎖出這些工具。

### Option C:HashiCorp Nomad

- **Pros:** 單一 binary、輕量、操作模型簡單。
- **Cons:** ML 社群較小；GPU + CSI 的 ergonomics 落後 Kubernetes；得重學一套在多數職場用不上的 pattern，等於丟掉既有的 `kubectl` 肌肉記憶。

### Option D:K3s

- **Pros:** 完整 Kubernetes API 相容（`kubectl`、helm、所有標準 manifest 都能用）。單一 binary,control-plane 約 ~512 MB 即可。預先綁好 `traefik`、`local-path-provisioner`、`coredns`、`metrics-server` — 第 0 天就少很多 moving part。原生支援 arm64 + x86；在 edge / IoT 叢集上被 Rancher 長期實戰。
- **Cons:** 部分 alpha 級 k8s 功能被砍掉（對 home lab 都不相關）。內嵌 etcd 或 sqlite — 此處選 etcd,snapshot 流程比較熟悉。

## 3. 最終決策與核心理據

選擇 **Option D:K3s**。

> **IMPORTANT**
> **技術理由：** K3s 是唯一同時滿足以下三點的候選：(a) 符合 laptop node 的 RAM 預算、(b) 保留完整 `kubectl` / helm / 標準 manifest 相容、(c) 留住整個 Kubernetes ML/MLOps 生態系的進場機會。Vanilla k8s 的 RAM 開銷會吃進 `node-b` 那 30 GB 我們明確需要留給 Postgres + pgvector 的空間。Swarm 與 Nomad 會把平台跟主流 ML 工具鏈隔開 — 這個策略成本遠大於它們帶來的操作簡潔性。

## 4. 後續影響

- **Positive:** 比起 vanilla k8s 省下 ~1.5 GB RAM，直接釋放給 `node-b` 上的有狀態 workloads。內建的 `local-path-provisioner` 第 0 天就免去 CSI driver 選型。`dev-mac` 上的 `kubectl` 可以透過 Tailscale 直連叢集（見 ADR-003 · 規劃中）。
- **Negative/Risk:** K3s 內建的 `traefik` 可能跟 `node-c` 上既有的獨立 `traefik`（目前在 docker-compose）衝突。計畫：啟動時 `--disable traefik`，改把 `node-c` 那個 instance 升格為 cluster ingress。
- **連動決策：** 單一 control-plane 的選擇（ADR-002 · 規劃中）是建立在 K3s 輕量足跡之上才合理。網路平面選型（ADR-003 · 規劃中）則取代了 K3s 預設的 flannel-over-LAN。
