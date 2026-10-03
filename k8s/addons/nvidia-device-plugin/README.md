# NVIDIA device plugin v0.20.1

`upstream.yaml` 原封不動保存 [NVIDIA v0.20.1 官方 static manifest](https://github.com/NVIDIA/k8s-device-plugin/blob/v0.20.1/deployments/static/nvidia-device-plugin.yml)，
授權為 Apache-2.0（附 LICENSE）。K3s 差異只在 `k3s.yaml`：
指定 NVIDIA runtime、GPU 節點 selector、初始化失敗時報錯及 CPU／RAM 資源。

本 addon 不包含驅動或 Container Toolkit，也不由 app overlay 自動部署。
從 infra repo 手動預覽／安裝一次即可，不需 Helm：

```bash
make k8s-gpu-render
make k8s-gpu-install KUBE_CONFIG="$HOME/.kube/finance-k3s.yaml"
kubectl -n kube-system get pods -l name=nvidia-device-plugin-ds -o wide
kubectl get node finance-gpu -o jsonpath='{.status.allocatable.nvidia\.com/gpu}{"\n"}'
make k8s-doctor
```

安裝前 B 要有 `finance.node/role=gpu`、可用的 `nvidia-smi` 及 `nvidia-container-runtime`，
K3s 要已偵測到 runtime，叢集須有 `RuntimeClass/nvidia`。
若已有 Helm／GPU Operator 管理的 plugin，沿用該管理方式，不要同時部署第二份 plugin。
若原本使用同名 static DaemonSet，此設定保留 selector，可就地更新；先 diff 確認。

更新時從指定 release 重新取得 upstream manifest 與 LICENSE，檢查 upstream 差異，
再更新文件版本並執行 render/schema 驗證；不追蹤 latest 或 main。
GPU 主機仍需實測驅動相容性、插件回報與 STT 推論。
