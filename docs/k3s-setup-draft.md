# K3s 單節點部署與遷移筆記 (Draft)

> 這份文件記錄了從 Docker Compose 遷移至 K3s 單節點，並且開啟 GPU 支援、部署 K8s Dashboard 的完整流程。

## 1. 備份原有資料與關閉 Compose

因為 K3s 會跑在同樣的 Port 上（例如 Postgres 5432），要先暫停原來的服務：

```bash
# 從 Docker 裡的 Postgres 匯出資料庫
docker compose exec postgres pg_dump -U admin -d finance_db -Fc -f /tmp/dump.sql
docker compose cp postgres:/tmp/dump.sql ./finance_db.dump

# 停掉 Docker Compose
docker compose down
```

## 2. 安裝 K3s 與開啟 GPU 支援

K3s 預設的 containerd 不認得 NVIDIA 驅動，我們需要覆寫預設引擎，並且幫 K8s 註冊 NVIDIA 的 RuntimeClass：

```bash
# 安裝 K3s
curl -sfL https://get.k3s.io | sh -

# 建立 K3s 的 containerd 外掛設定資料夾
sudo mkdir -p /var/lib/rancher/k3s/agent/etc/containerd/config.toml.d/

# 寫入強制把預設容器引擎 (runc) 替換為 nvidia 的設定
sudo bash -c 'cat <<EOF > /var/lib/rancher/k3s/agent/etc/containerd/config.toml.d/00-default-nvidia.toml
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc]
  runtime_type = "io.containerd.runc.v2"
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc.options]
  BinaryName = "/usr/bin/nvidia-container-runtime"
EOF'

# 重啟 K3s 讓設定生效
sudo systemctl restart k3s

# 安裝 NVIDIA K8s device plugin
sudo k3s kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.17.0/deployments/static/nvidia-device-plugin.yml

# 註冊 NVIDIA RuntimeClass
sudo bash -c 'cat <<EOF | sudo k3s kubectl apply -f -
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: nvidia
handler: nvidia
EOF'

# 關鍵一步：更新 NVIDIA device plugin 讓它也使用 nvidia 通道，否則它自己會看不到實體 GPU
sudo k3s kubectl patch ds nvidia-device-plugin-daemonset -n kube-system -p '{"spec": {"template": {"spec": {"runtimeClassName": "nvidia"}}}}'
```

## 3. 部署基礎設施與機密資料

```bash
# 建立 namespace 與套用 secrets
sudo k3s kubectl apply -f k8s/namespace.yaml
sudo k3s kubectl apply -f k8s/secrets.yaml

# 建立拉取 GHCR 映像檔的憑證 (替換為真實 PAT)
sudo k3s kubectl create secret docker-registry ghcr-cred \
  --docker-server=ghcr.io --docker-username=pei9564 \
  --docker-password=<YOUR_GHCR_PAT> \
  -n finance --dry-run=client -o yaml | sudo k3s kubectl apply -f -

# 部署基礎設施
sudo k3s kubectl apply -f k8s/postgres.yaml
sudo k3s kubectl apply -f k8s/redis.yaml
sudo k3s kubectl apply -f k8s/vllm-external.yaml
```

## 4. 還原資料庫

```bash
# 等待 Postgres Pod 啟動
sudo k3s kubectl wait --for=condition=ready pod -l app=postgres -n finance --timeout=120s

# 把第一步備份的 dump 檔倒進去
sudo k3s kubectl cp ./finance_db.dump finance/postgres-0:/tmp/dump.sql
sudo k3s kubectl exec -n finance postgres-0 -- pg_restore -U admin -d finance_db --clean --if-exists /tmp/dump.sql
```

## 5. 部署應用程式

```bash
sudo k3s kubectl apply -f k8s/scraper.yaml
sudo k3s kubectl apply -f k8s/stt.yaml

# 檢查 Pod 狀態
sudo k3s kubectl get pods -n finance
```

> **避坑指南 (Rolling Update 資源死鎖)**：
> 單節點只有 1 張顯卡時，如果在 `stt.yaml` 使用預設的 `RollingUpdate`，更新時 K8s 會試圖同時開舊 Pod 與新 Pod，導致新 Pod 因為搶不到顯卡卡死在 `Pending`。
> 解決方法是在 Deployment 中加上 `strategy: type: Recreate`，讓 K8s 先殺掉舊的、再開新的，確保顯卡能順利釋放與重新分配。

## 6. 部署 Kubernetes Dashboard

圖形化管理介面，包含建立管理員權限：

```bash
# 部署官方 Dashboard
sudo k3s kubectl apply -f https://raw.githubusercontent.com/kubernetes/dashboard/v2.7.0/aio/deploy/recommended.yaml

# 套用 admin-user 權限綁定 (檔案: k8s/dashboard-admin.yaml)
sudo k3s kubectl apply -f k8s/dashboard-admin.yaml

# 產生登入用的 Token (複製印出來的亂碼)
sudo k3s kubectl -n kubernetes-dashboard create token admin-user

# 開啟 proxy 讓外部可以連線
sudo k3s kubectl proxy
```

進入 Dashboard：打開瀏覽器訪問 `http://localhost:8001/api/v1/namespaces/kubernetes-dashboard/services/https:kubernetes-dashboard:/proxy/`，選擇 Token 登入。
