# 兩節點 K3s＋Tailscale

目標：A 是長期開機、有 SSD 的 server（PostgreSQL／scraper），B 是 NVIDIA GPU agent（STT）。
這是分散工作負載的叢集，A 仍是控制平面與 DB 的單點。
以下是 Linux＋systemd、新叢集的設定範例；現有叢集先備份，不能直接覆蓋未知的 K3s 設定。

## 1. 準備 Tailscale 網路

兩台先安裝 Tailscale 並加入同一 tailnet，確認 `tailscale0` 存在。
各自執行 `tailscale ip -4` 取得自己的 100.x IP，再互相 `tailscale ping <對方 IP>`。
使用 `tailscale status` 確認 direct／relay；跨地點或 relay 的延遲會影響 DB 存取與檔案傳輸。

Tailscale grants／ACL 與主機防火牆必須允許：

| 流量 | 方向 |
|---|---|
| TCP 6443 | B／管理電腦 → A，K3s join 與 API |
| UDP 8472 | A ↔ B，Flannel VXLAN |
| TCP 10250 | A ↔ B，kubelet／metrics |

這些是 tailnet 內的規則，UDP 8472 不開到公網。
Tailscale 在主機之間提供加密；本範例讓 Flannel VXLAN 經過 tailscale0。
Pod／Service CIDR 必須避免和兩端 LAN、VPN subnet routes 衝突；
範例使用 K3s 預設的 10.42.0.0/16 與 10.43.0.0/16，若衝突要在建立叢集前修改。
Tailscale 必須先啟動且連線正常，才啟動 K3s。

## 2. A：K3s server

在 A 建立 `/etc/rancher/k3s/config.yaml`（把 placeholder 換成自己的 IP）：

```yaml
node-name: finance-data
node-ip: "<A 的 Tailscale IPv4>"
advertise-address: "<A 的 Tailscale IPv4>"
tls-san:
  - "<A 的 Tailscale IPv4>"
flannel-backend: vxlan
flannel-iface: tailscale0
node-label:
  - finance.node/role=data
```

使用官方安裝腳本安裝，兩台使用相同且固定的 K3s 版本：

```bash
# 先選擇要部署的 K3s release，例如已驗證的 vX.Y.Z+k3s1
export INSTALL_K3S_VERSION='<選定的 K3s release>'
curl -sfL https://get.k3s.io | sh -s - server
```

加入 B 時需要 A 的 `/var/lib/rancher/k3s/server/node-token`，用安全方式傳給 B。
不要把 token、kubeconfig 或真實主機設定 commit 到 repo。

## 3. B：GPU runtime 與 agent

先安裝符合主機 GPU 的 NVIDIA 驅動及 Container Toolkit，確認：

```bash
nvidia-smi
command -v nvidia-container-runtime
```

K3s 啟動時會偵測 NVIDIA runtime，不需要沿用舊文件裡強制覆寫預設 runc 的設定。
驅動／Toolkit 的安裝請依 [NVIDIA 官方文件](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)。

B 的 `/etc/rancher/k3s/config.yaml`：

```yaml
server: "https://<A 的 Tailscale IPv4>:6443"
token-file: /etc/rancher/k3s/join-token
node-name: finance-gpu
node-ip: "<B 的 Tailscale IPv4>"
flannel-iface: tailscale0
node-label:
  - finance.node/role=gpu
```

將 A 的 join token 存入 `/etc/rancher/k3s/join-token`，設定 root 擁有、權限 600。
再安裝相同版本：

```bash
export INSTALL_K3S_VERSION='<與 A 相同的 K3s release>'
curl -sfL https://get.k3s.io | sh -s - agent
```

若先安裝 K3s 後才裝 Toolkit，重啟 B 的 `k3s-agent`。
本 repo 保存固定版 **v0.20.1** 的官方 device plugin manifest，透過 Kustomize 加入
NVIDIA RuntimeClass 與 `finance.node/role=gpu` selector，不需要額外安裝 Helm。
在管理電腦設定 kubeconfig 後執行：

```bash
make k8s-gpu-render
make k8s-gpu-install
kubectl -n kube-system get pods -l name=nvidia-device-plugin-ds -o wide
```

安裝詳細條件、upstream 來源與更新方式見 [GPU addon 文件](../k8s/addons/nvidia-device-plugin/README.md)。
若已有 GPU Operator／Helm 管理的 device plugin，先確認現有配置，不要重複部署。

完成後從有叢集存取權的電腦確認：

```bash
kubectl get nodes -o wide
kubectl get runtimeclass nvidia
kubectl get node finance-gpu -o jsonpath='{.status.allocatable.nvidia\.com/gpu}'
```

B 必須回報至少一張 GPU，再部署 homelab overlay。

## 4. 管理電腦的 kubeconfig

安全複製 A 的 `/etc/rancher/k3s/k3s.yaml`，存成自己可讀、權限 600 的 kubeconfig。
將其中 server 的 `127.0.0.1` 改為 A 的 Tailscale IP，保留 CA 驗證。
不要用 `insecure-skip-tls-verify`；A 的 IP 已由 tls-san 納入憑證。

```bash
export KUBECONFIG=/path/to/finance-k3s.yaml
kubectl config current-context
kubectl get nodes -o wide
```

節點 Ready 之後，還需確認跨節點 Pod、Service DNS 與 DB 連線。
依 [Kustomize 部署文件](../k8s/README.md) 建立 secrets、pin images、diff、deploy。
部署後可在兩個 app Pod 各自驗證 PostgreSQL TCP 連線：

```bash
kubectl -n finance exec deployment/finance-scraper-service -- python -c 'import socket; socket.create_connection(("postgres", 5432), timeout=5).close()'
kubectl -n finance exec deployment/stt-ai-worker -- python -c 'import socket; socket.create_connection(("postgres", 5432), timeout=5).close()'
kubectl -n finance exec deployment/finance-scraper-service -- python -c 'import urllib.request; print(urllib.request.urlopen("http://stt-ai-worker:8000/readyz", timeout=10).status)'
```

第一個驗證 A 上的 DB，第二／三個驗證跨 A、B 的 Pod 路徑及 Service DNS。
模型未載入時先等 STT startup；連線異常再檢查 ACL、防火牆、Flannel 介面與 MTU。

## 5. 存取與維運

初期使用 `make k8s-forward`，在管理電腦開啟 http://localhost:8080。
之後需要固定的 tailnet 網址，再安裝 Tailscale Operator 暴露 scraper。
Operator 用來提供服務入口，不代替上述節點之間的 Flannel 網路。

PostgreSQL PVC 固定在 A，音訊 PVC 固定在 B；Tailscale 不會複製磁碟資料。
定期將 DB dump 與需要保留的音訊備份到不同故障域。
兩台電腦任一休眠或 VPN 中斷都可能讓應用無法完成工作，請關閉服務主機自動休眠。

## 官方參考

- [K3s 網路介面與 Flannel 設定](https://docs.k3s.io/networking/basic-network-options)
- [K3s 網路連接埠需求](https://docs.k3s.io/installation/requirements#networking)
- [K3s NVIDIA runtime 支援](https://docs.k3s.io/advanced#nvidia-container-runtime)
- [K3s local-path 儲存](https://docs.k3s.io/add-ons/storage)
- [Tailscale Kubernetes Operator](https://tailscale.com/docs/kubernetes-operator)
