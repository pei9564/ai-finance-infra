---
title: "📜 ADR-006:test / production 環境隔離策略"
---

# ADR-006:test / production 環境隔離策略

- **Status:** 🟡 Proposed(K3s 落地後翻 🟢 Accepted;前置 [ADR-001](?file=adr/adr-001-k3s-selection.md))
- **Date:** 2026-05-23
- **Decider:** Pei

## 1. 背景與面臨挑戰

[ADR-001](?file=adr/adr-001-k3s-selection.md) 選定 K3s 之後,下一個必答題:**怎麼把「正在跑的 ingestion」跟「驗證新版的環境」切開**。

目前 (docker-compose 階段) 沒有環境概念 — image 改完直接 watchtower 拉 `:latest` 進 prod 容器,改壞就壞了。轉到 K3s 是把這個漏洞補上的時機。

需求 / 約束:

- **單人 dev,但 ingestion 已是「實際在跑」的 workload** — 抓壞 24 小時資料可以,但不能弄丟摘要邏輯
- **硬體預算極緊**(見[硬體基線](./)) — `node-b` 的 ~32 GB 是唯一夠扛 control-plane + Postgres + pgvector 的節點,沒有第二個位置擺第二個 control-plane
- **GPU 是唯一的**(`node-a` 上一張 Ampere 4 GB) — test 跟 prod 不能同時各佔一個 STT pod
- **生態系不能切斷** — kubectl / helm / argo / kustomize 都要能直接用,自架工具的成本承擔不起

## 2. 評估候選方案

### Option A:兩個獨立 K3s cluster(physical isolation)

- **Pros:** 隔離最強;control-plane / etcd / CNI 完全獨立;test 把 cluster 玩爆不影響 prod;cluster-scoped 物件 (CRD / ClusterRole / IngressClass) 不會撞名。
- **Cons:** 第二個 control-plane 至少再吃 ~512 MB RAM + etcd 開銷,在 `node-b` 上撐不起;GPU 在哪個 cluster 就只服務哪個,test 想跑 GPU STT 就要跨 cluster 借資源(沒有原生機制);兩套 image registry 認證 / RBAC / monitoring 都得各自設一份,維運成本翻倍。對 4 節點 home lab 是奢侈品。

### Option B:單一 cluster + namespace 隔離(`finance-prod` / `finance-test`)

- **Pros:** K8s primitive,工具鏈零摩擦(`kubens` 切 context、Argo per-app namespace、helm `--namespace` 都是日常用法);共用 control-plane / GPU / 儲存,4 節點預算撐得住;ResourceQuota + LimitRange + NetworkPolicy 可以把「邏輯隔離」拉到實務上夠用的等級;K3s 預設就帶 NetworkPolicy controller(內建 kube-router),不用換 CNI。
- **Cons:** 是「邏輯」隔離不是「硬」隔離 — node 硬體、etcd、cluster-scoped 物件仍共享;test 把 etcd 寫爆(例如猛建 ConfigMap)會打到 prod;GPU 仍是排他資源,test pod 要 GPU 時得排隊。

### Option C:vcluster(virtual cluster on shared K3s)

- **Pros:** 每個 tenant 一個 virtual API server,看起來像自己的 cluster,CRD / ClusterRole 不互撞;比 Option A 省 control-plane RAM。
- **Cons:** 多一層抽象、多一套需要學的 failure mode;適合 SaaS / 多 tenant 平台場景,單人 home lab 用不到那個強度的隔離;社群 / 文件密度遠不如純 namespace pattern;額外 dependency,違反「ML/MLOps 生態系工具直接能用」這條 K3s 選型本來的核心理由([ADR-001](?file=adr/adr-001-k3s-selection.md) §3)。

## 3. 最終決策與核心理據

選擇 **Option B:單一 cluster + namespace 隔離**。

具體分割:

- Namespaces: `finance-prod` / `finance-test`(`kube-system` / `monitoring` / `argocd` 等 platform-level 不在此 scope)
- 每個 ns 各自有 ResourceQuota(限 CPU / RAM / `nvidia.com/gpu` 上限)
- 每個 ns 各自有 LimitRange(無 request/limit 的 pod 給 default)
- NetworkPolicy:預設 deny-all,顯式 allow `finance-test` → `finance-test`,`finance-prod` → `finance-prod`,跨 ns 禁止
- Postgres:同一個 StatefulSet,**兩個 database**(`finance_prod` / `finance_test`),各自獨立 user。不開第二個 instance(`node-b` RAM 預算不夠)
- Image tag 策略:`finance-prod` 鎖 `:sha-xxxxxxx`(immutable digest),`finance-test` 才能跑 `:latest`
- GPU:`nvidia.com/gpu: 1` 同一時刻只能給一個 pod,prod 跑生產 ingestion 時 test 的 STT pod 縮到 0(透過 Argo / kubectl scale,不自動)

> **IMPORTANT**
> **技術理由:** 物理 cluster(Option A)的 RAM 成本直接撞到 `node-b` ~32 GB 的硬天花板,而 4 節點 home lab 的隔離需求不到那個強度 — 「test 不要爆到 prod」用 ResourceQuota + NetworkPolicy 就能擋住 90% 的失敗情境,剩下 10%(cluster-scoped 物件、etcd) 用「test 不裝 CRD / 不亂建大量物件」這條開發紀律補。vcluster(Option C) 是為 multi-tenant SaaS 設計的,在這個場景把單純的 namespace pattern 換成多一層 virtual API server,只是把 K3s 選型本來節省的 moving part 補回來。Namespace 是 K8s primitive,所有工具(kubectl / helm / Argo / kustomize) 零摩擦支援。

## 4. 後續影響

- **Positive:** 維運成本固定在 1 套 cluster;`kubens finance-test` 切過去就能跑驗證,改完 `kubens finance-prod` 看一眼確認沒污染;image tag 鎖 digest 之後,prod 不會被 watchtower / 任何人不小心推上去的 `:latest` 影響(這是現在 compose 階段最大的漏洞);test ns 可以放心拿來做 schema migration / 換模型 / 試 ArgoCD sync 策略。
- **Negative/Risk:** GPU 排他仍在 — test 跑 STT 時 prod 的 STT 必須暫停,反之亦然;若同時想 idle prod 跑 ingestion + test 驗證新版 STT,只能 test STT 走 CPU 模式(慢但能跑);etcd 共享 → test 不能亂建幾千個 ConfigMap;cluster-scoped 物件(IngressClass / CRD) 仍是「全 cluster 唯一」,引入 Argo / Strimzi 這類帶 CRD 的工具時 prod 跟 test 共用同一份 CRD 版本,升級時無法兩個 ns 不同版。
- **量化(實跑後量,翻 🟢 的條件):** 兩個 ns 同時 idle 的 cluster RAM 佔用 = `[待量測]`;test ns ResourceQuota 觸發 OOM 時 prod 的 latency 漂移 = `[待量測]`;test GPU pod scale-up → prod STT scale-down 切換時間 = `[待量測]`。
- **連動決策:** 編排器選型 [ADR-001](?file=adr/adr-001-k3s-selection.md) 是本決策的前置;NetworkPolicy 的 enforcement 依賴 ADR-003 網路平面選型(規劃中) 最終 CNI 選擇;image tag 從 `:latest` → digest 的工作流改造會推動 GitOps 工具選型(未來 ADR-007 草稿,取代目前 watchtower 流程)。
