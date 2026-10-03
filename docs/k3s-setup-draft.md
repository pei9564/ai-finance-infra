# K3s 部署文件

原本的單節點草稿已由以下文件取代：

- [K3s＋Tailscale 兩節點安裝](k3s-tailscale-setup.md)
- [Kustomize 應用部署、儲存與遷移](../k8s/README.md)

應用統一透過 `make k8s-diff`／`make k8s-deploy` 部署。
不再逐一 apply 舊的頂層 manifests，也不沿用舊的 containerd 覆寫與 Dashboard 安裝流程。

## K3s 備份與還原

已在 K3s 執行的系統，使用 [DB＋uploads 備份還原工具](backup-and-restore.md)：
`make k8s-backup`／`make k8s-restore BACKUP_SOURCE=...`。
下方手動流程僅供從 Compose 遷移至 K3s。

## Compose 資料遷移

使用自己設定的資料庫使用者與名稱。先停掉會寫入資料的應用，再匯出資料庫；
以下從容器環境讀取 POSTGRES_USER／POSTGRES_DB，不會列出密碼：

```bash
docker compose stop finance-scraper-service stt-ai-worker
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > finance_db.dump
```

確認 dump 成功且非空，再備份 uploads；`make stop` 可停止其餘容器並保留 volumes。
若使用 local／dev Compose 模式，以上 Docker Compose 指令需帶上相同 override files。

在目的叢集建立 namespace、Secrets 後，先在選定 overlay 的 `kustomization.yaml`
暫時加入以下設定，確保還原期間應用不會先啟動或恢復工作：

```yaml
replicas:
  - name: finance-scraper-service
    count: 0
  - name: stt-ai-worker
    count: 0
```

```bash
make k8s-diff
make k8s-deploy
kubectl -n finance rollout status statefulset/postgres --timeout=180s
kubectl -n finance cp finance_db.dump postgres-0:/tmp/finance_db.dump
```

以下還原會覆寫目的資料庫已有物件，只對預定的遷移目的 DB 執行：

```bash
kubectl -n finance exec postgres-0 -- sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists /tmp/finance_db.dump'
```

STT 未完成工作的來源可能指向 `/data/uploads`，所以 DB dump 之外，
也要在停機狀態備份／還原 Compose uploads volume 到 B 的 PVC，保留檔名與路徑。
請用暫時的維護 Pod（排到 B，掛載 stt-uploads PVC）完成還原，並確認檔案權限。
只還原 DB 不足以恢復上傳工作。

音訊與 DB 還原完成後，移除暫時的 `replicas` 設定，再執行 `make k8s-diff`／`make k8s-deploy`。
確認應用、舊資料與工作恢復正常後，才處理原有 Compose 資源。
