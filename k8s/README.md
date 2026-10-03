# Kustomize 部署

本目錄使用 kubectl 內建 Kustomize，不需 Helm 或 values.yaml。
首次執行請照 [主 README 的逐步部署流程](../README.md#k3skustomize實際執行流程)，本文件補充設定與遷移細節。

- `base/`：共用資源、ConfigMap generators；STT 預設 CPU。
- `overlays/homelab/`：兩節點部署，data 節點跑 PostgreSQL／scraper，gpu 節點跑 STT。
- `overlays/cpu/`：無 GPU 測試；不限制節點、不需要 NVIDIA RuntimeClass。
- `optional/vllm-external/`：外部 vLLM 代理，預設不部署。
- `secrets.yaml.example`：Secret 範本；真實檔案不進 Git，也不納入 Kustomize。

## 前置設定

先依 [K3s＋Tailscale 安裝文件](../docs/k3s-tailscale-setup.md) 完成節點連線和 GPU 設定。
所有 make 指令在 infra repo 執行。以下 kubectl 指令都使用已設定的 kubeconfig／context；
可用 `make ... KUBE_CONTEXT=<context>` 指定 Makefile 目標的叢集。

Makefile 的 kubeconfig 選擇順序：明確的 `KUBE_CONFIG=/path` → 已 export 的 `KUBECONFIG`
→ 存在的 `$HOME/.kube/finance-k3s.yaml` → kubectl 預設。
不會自動複製或改變 `/etc/rancher/k3s/k3s.yaml` 的權限。
`diff`／`deploy` 執行前都會顯示 context、API server 與 overlay；`k8s-target` 可單獨查看。

```bash
make k8s-target KUBE_CONFIG="$HOME/.kube/finance-k3s.yaml"
make k8s-deploy KUBE_CONFIG="$HOME/.kube/finance-k3s.yaml" KUBE_CONTEXT=<context>
```

原生 kubectl 指令仍需 export KUBECONFIG 或使用自己的 `--kubeconfig`。

```bash
kubectl config current-context
kubectl label node <A-node-name> finance.node/role=data --overwrite
kubectl label node <B-node-name> finance.node/role=gpu --overwrite
kubectl apply -f k8s/base/namespace.yaml
cp k8s/secrets.yaml.example k8s/secrets.yaml
# 編輯 secrets.yaml，填入自己的 DB、GitHub、LLM credentials
kubectl apply -f k8s/secrets.yaml
```

Private GHCR image 另外需要 finance namespace 的 `ghcr-cred`。
本機的 `docker login` 不會自動讓 K3s containerd 登入。可使用一份專用 Docker config
建立 Secret，避免把自己的其他 registry credentials 一併匯入：

```bash
registry_config=$(mktemp -d)
docker --config "$registry_config" login ghcr.io --username pei9564
kubectl -n finance create secret generic ghcr-cred \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson="$registry_config/config.json" \
  --dry-run=client -o yaml | kubectl apply -f -
rm -rf "$registry_config"
```

請確認 Docker config 使用內嵌 auth；若使用 credential helper，先依
[Kubernetes image pull Secret 文件](https://kubernetes.io/docs/tasks/configure-pod-container/pull-image-private-registry/)
建立完整的 registry credential。若 images 全部公開，可移除 base 的 imagePullSecrets。

## 環境調整

部署前編輯 `overlays/homelab/kustomization.yaml` 的 `images[].newTag`，
目前固定為 scraper `sha-33734b8`、STT `sha-9153e89`；兩個 app 的 SHA 可以不同。
本地尚未 push 的 commit 不會有可拉取的 image。

模型／LLM 設定可在 overlay 加 `configMapGenerator`（`behavior: merge`），
沿用 homelab 的 DEVICE 範例；ConfigMap hash 改變後會觸發 Deployment 更新。
節點位置調整 `placement.yaml`，GPU runtime／資源調整 `gpu.yaml`。
共享的 PVC 容量在 base 設定；現有 PVC 要擴容需 storage class 支援，不能縮小。

CPU overlay 保留相同資源名稱與 PVC，適合另一個測試叢集；不是另一份可並存的安裝。
不要在已有 GPU uploads PVC 的正式叢集直接切換 CPU overlay，除非先處理節點／資料位置。

## 預覽與部署

```bash
make k8s-check                         # 離線 render 所有設定
make k8s-render                        # 預設 homelab
make k8s-render K8S_OVERLAY=cpu
make k8s-doctor KUBE_CONTEXT=<context>  # read-only preflight
make k8s-diff KUBE_CONTEXT=<context>    # 有差異時 kubectl diff exit code 為 1
make k8s-deploy KUBE_CONTEXT=<context>
make k8s-status KUBE_CONTEXT=<context>
make k8s-logs KUBE_CONTEXT=<context> K8S_APP=stt-ai-worker
make k8s-forward KUBE_CONTEXT=<context> # http://localhost:8080
```

`k8s-check` 只驗證 render，不會連線叢集；`diff` 才會請 API server 驗證實際資源。
GitHub Actions 的 `kustomize.yml` 另使用 kubeconform 驗證 Kubernetes 1.35 schema。
`k8s-deploy` 只 apply，不執行 prune 或刪除 PVC。未提供自動解除安裝／刪除資料指令。
所有 Service 保持 ClusterIP；初期用 port-forward 存取，未來再加 Tailscale Operator 入口。
Redis 保留現有設定，但應用尚未使用它。

## 儲存與更新

PostgreSQL 與 STT uploads 使用 local-path PVC，綁定原節點。
節點 labels 不會搬移既有 volume，A／B 故障時需修復原節點或從備份還原。
Whisper cache 使用 GPU 主機的 `/opt/hf-cache`，可以重新下載。

STT 始終單副本、Recreate，單一程序內工作佇列不支援多 Pod 競爭工作。
更新時會短暫中斷；未完成工作由 PG 恢復後從頭執行。
回滾方式是回復 Git 中的 image/config，再重新 diff／deploy；這不會回滾 DB 資料。

## 從原 manifests 遷移

資源名稱、namespace、Deployment selectors、PVC 名稱與 StatefulSet volumeClaimTemplates
都保留。先備份 DB／uploads，確認既有 PV 的 node affinity，再執行 diff。
如果原本都跑在同一台，新增 homelab selectors 不能自動把資料搬到 B；
先規劃停機複製／還原 uploads 到 B 的新 volume，或暫時修改 STT placement 使用原節點的 hostname selector，
直到資料遷移完成。DB 的 data label 同理。

舊 `scraper-config`／`stt-config` 會改由帶 hash 的 ConfigMap 取代；apply 不會自動刪除舊 ConfigMap。
確認 rollout 成功後可手動清理未引用的 ConfigMap。其餘資源由相同名稱就地更新。

## 可選外部 vLLM

預設使用雲端相容 API，不會部署外部 vLLM 的示例 IP。
需要時，先修改 `optional/vllm-external/service.yaml` 的 IP 為 Pod 可達的主機位址，
再把該目錄加入 overlay 的 resources（`../../optional/vllm-external`），
並合併 scraper-config 的 `VLLM_BASE_URL=http://vllm-gateway:8000/v1` 與 `VLLM_MODEL`。
只有主機可達的 MagicDNS 名稱不代表 Pod 也能解析，請實測 Pod 到 gateway 的連線。

`make k8s-doctor` 會檢查選定 context、Ready／可排程節點、homelab 的 data／gpu labels、
GPU allocatable 數量、NVIDIA RuntimeClass、local-path、應用 Secret 的鍵名及 GHCR pull Secret 類型。
缺少必要設定時回傳非零；未建立的 PVC 與等待第一個 Pod 的 Pending PVC 會提示。
此檢查需要 Python 3，不讀取／輸出 Secret 值，也不驗證 credential 是否有效或跨節點網路。

應用 image pins 由 `make k8s-check` 與 CI 檢查，拒絕 latest／main 等未固定版本。
目前目標為 scraper `33734b89873d61e9430f5e801a906ed617607cd1`、
STT `9153e8917e9ff09014a101f967d4c1d5e1f9416e`。
這是部署目標，不代表已發布；GHCR image 可用性仍需發布 CI／實際 pull 確認。
SHA tag 依 CI 的版本慣例追蹤 commit；要完全綁定 image 內容，可改用 Kustomize images 的 `digest: sha256:...`。

## 備份與還原操作

```bash
make k8s-backup
make k8s-restore BACKUP_SOURCE=/path/to/completed-bundle
```

備份包含 DB 與 uploads，會暫停 app 後恢復原副本數；還原前先驗證完整性，
預設拒絕覆寫非空目的端。只有明確要取代資料才加 `RESTORE_REPLACE=1`。
還原失敗不會重啟 app。首次還原請先將目的 app 設為 0，避免預先初始化／恢復工作。
完整流程、異地保存與失敗處理見 [備份與還原文件](../docs/backup-and-restore.md)。
