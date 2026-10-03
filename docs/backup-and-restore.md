# PostgreSQL＋uploads 備份與還原

在有叢集權限的管理電腦執行。工具需要 Python 3、kubectl；
採用 `KUBE_CONFIG`／`KUBE_CONTEXT` 和其他 Makefile 目標相同的選擇方式。
資料從叢集串流到管理電腦，備份不會只留在 A／B 的磁碟上。

## 備份包含什麼

每個 bundle 含有：

- `postgres.dump`：整個應用 DB 的 PostgreSQL custom-format dump，不含 roles／ownership／ACL。
- `uploads.tar.gz`：stt-uploads PVC 的內容，保留相對檔名與目錄結構。
- `metadata.json`：格式版本、UTC 時間、原副本數、app image 版本與檔案 SHA256。
- `configuration.json`（新版）：工作負載及所引用的 ConfigMaps／Secrets，包含敏感設定。

新版包含應用引用的 Secrets（見下方 configuration.json 說明）；不包含 K3s 控制平面 datastore、Redis 或可重新下載的模型 cache。
請另外安全保存 kubeconfig；K3s 本身的備份依官方 datastore 流程處理。
備份完整性用 checksum 檢查，沒有加密；複製到外部儲存前依需要加密。
預設 `backups/` 不進 Git，bundle 目錄權限 700，工具以 umask 077 建立檔案。

## 1. 執行備份

先確認 PostgreSQL、兩個 Deployments 與 stt-uploads PVC 存在，管理電腦有足夠磁碟空間。
叢集需要能拉 `busybox:1.37.0` 作為短暫維護 Pod。
當次備份會停掉兩個 app，因此網頁／新任務會短暫無法使用。

```bash
make k8s-target
make k8s-doctor
make k8s-backup
```

預設寫入 `backups/<UTC timestamp>/`。也可自行指定全新目錄：

```bash
make k8s-backup BACKUP_DIR=/path/to/private-backups/finance-20261003
```

工具會建立 maintenance lock、記錄副本數、scale scraper／STT 為 0、等待 Pod 刪除，
再用 PostgreSQL Pod 的 pg_dump 及唯讀維護 Pod 匯出音訊。
維護 Pod 依已綁定的 PVC 排程到資料所在節點；新 homelab PVC 的第一個 consumer 指定到 GPU 節點。
完成後刪除維護 Pod，恢復原副本數並釋放 lock。
STT 執行中的任務會中斷，啟動後由 PG 恢復並從頭執行。

失敗的 backup 沒有 `metadata.json`，不能當成完成的 bundle；app 副本數仍會嘗試恢復。
請看退出狀態與 `make k8s-status`，不要只靠目錄存在判斷成功。
另一個備份／還原已持有 lock 時，工具會退出，不會變更它的副本數或移除其 lock。
維護期間不要另外 deploy／scale app，也不要讓其他程式寫入相同 DB／uploads；
若之後有 GitOps／HPA，先暫停它們的自動調整。

## 2. 保存到另一個故障域

完成後把整個 bundle 複製到另一台電腦、NAS 或外部儲存，保留 metadata.json：

```bash
rsync -a --chmod=D700,F600 backups/<timestamp>/ <backup-user>@<backup-host>:/private-backups/finance/<timestamp>/
```

替換 placeholders，確認目的端收到完整檔案。只在 A 的資料磁碟保留 dump 無法保護 A 磁碟故障。
建議保留數個歷史版本；工具不會自動刪除舊備份，也不會覆寫已存在的 bundle 目錄。

## 3. 在空白目的叢集還原

先建立可用的叢集、Secrets、相容版本的 app images 與 PostgreSQL／PVC。
部署前，在目的 overlay 的 kustomization.yaml 暫時加入以下設定：

```yaml
replicas:
  - name: finance-scraper-service
    count: 0
  - name: stt-ai-worker
    count: 0
```

```bash
make k8s-deploy
kubectl -n finance rollout status statefulset/postgres --timeout=180s
make k8s-restore BACKUP_SOURCE=/path/to/completed-bundle
```

先確認 kubeconfig 指向**目的叢集**。工具會在任何變更前驗證 checksum、dump 格式、
音訊 tar 的路徑及檔案類型；拒絕路徑穿越、symlink／hardlink、特殊裝置檔。
接著停掉 app、驗證 dump 可被目的 pg_restore 解析，並檢查目的 DB／uploads 是否空白。
預設遇到非空資料會退出，不會覆寫它。

DB 還原採單一 transaction，遇到 SQL 錯誤退出；uploads 以 tar 還原。
兩者不是跨 DB／檔案系統的原子 transaction：任一階段失敗，都不會恢復 app，
需修復／重試後再啟動，避免使用部分還原的資料。
若失敗時已有部分資料寫入，重試需明確使用 `RESTORE_REPLACE=1`；先保存目的端原有備份。
成功時恢復備份 metadata 裡的副本數；如果備份當時 app 原本就是 0，還原後也保持 0。
再移除目的 overlay 的暫時 replicas 設定，diff／deploy，確認啟動數量：

```bash
make k8s-diff
make k8s-deploy
make k8s-status
make k8s-logs K8S_APP=stt-ai-worker
```

新 homelab uploads PVC 會先由維護 Pod 在 GPU 節點掛載。
既有 local-path PVC 保留原節點位置，不會因還原或改 labels 自動搬到另一台。
若要換節點，先規劃新的 PVC／PV 與 placement，再還原到新的 volume。
使用 CPU 測試叢集時，所有相關 make 指令都帶 `K8S_OVERLAY=cpu`。

## 4. 明確取代目的端已有資料

先為目的端做一份備份。確定要用指定 bundle 取代目的端資料時：

```bash
make k8s-backup BACKUP_DIR=/path/to/pre-restore-backup
make k8s-restore BACKUP_SOURCE=/path/to/completed-bundle RESTORE_REPLACE=1
```

`RESTORE_REPLACE=1` 允許 pg_restore clean／重建備份中的 DB 物件，
並清空目的 uploads PVC 的內容後再還原。它不會刪除 PVC 或 PostgreSQL database；
備份未包含的其他 DB 物件也不會自動移除，因此優先使用空白目的 DB。
還原不會修改 app images／Secrets，請核對 metadata.json 的版本，使用相容程式與 DB schema。
不支援 DB major version 降版；跨 major version 請依 PostgreSQL 官方相容性說明處理。

## 5. 失敗後處理與最後驗證

工具失敗後先查看 app 是否停止、Pod／PVC 狀態，修正原因再重試。
不要在 uploads／DB 部分還原時直接重新 deploy app。
意外斷電或 SIGKILL 可能留下 lock／維護 Pod：

```bash
kubectl -n finance get configmap finance-maintenance-lock
kubectl -n finance get pods -l finance.maintenance=files -o wide
```

確認沒有其他備份／還原進行中，且 app 已停止，才手動刪除殘留維護 Pod 與 lock。
若維護 Pod 還存在，不要強制解鎖並啟動另一個還原。

最後統一測試時，在獨立測試叢集驗證：

1. 建立一個尚未完成的上傳工作與收藏，做備份。
2. 還原至空白 DB／PVC，確認檔名、工作來源、事件／收藏與逐字稿。
3. 啟動 STT，確認未完成工作恢復，成功後音訊清理。
4. 用毀損 bundle 測試拒絕還原，確認目的資料未改變。
5. 模擬還原失敗，確認 app 不會自動繼續寫入。

Docker 實際還原演練已完成（見下方）；兩台主機的 Kubernetes 備份傳輸與實際恢復仍需上述叢集整合測試。

官方參考：[pg_dump](https://www.postgresql.org/docs/15/app-pgdump.html)、
[pg_restore](https://www.postgresql.org/docs/15/app-pgrestore.html)。

## Docker Compose 備份與實際還原演練

```bash
make backup
make backup-verify BACKUP_SOURCE=backups/<timestamp>
make restore-drill BACKUP_SOURCE=backups/<timestamp>
```

`make backup` 暫停原本正在執行的 scraper、STT 與 Watchtower，保留 PostgreSQL 運行；完成或失敗後恢復原本運行狀態。使用檔案鎖避免同時備份／還原，不會改變目前使用 local 或 registry image 的容器。進行中的 AI 整理中斷後需重試，已保存結果不受影響。

Compose bundle 包含 `postgres.dump`、`uploads.tar.gz`、`configuration.tar.gz` 與 `metadata.json`。設定壓縮檔包含 `.env`、Compose／Kustomize 部署設定、Makefile 與維護腳本；可能含 API key／Secrets，整個 bundle 必須私密保存，不要提交到 Git。目錄權限 700、命令以 umask 077 建立檔案，未加密。模型 cache 與未使用的 Redis 不納入。

演練使用無對外 port、無外部網路的臨時 PostgreSQL 15 容器。它真正執行 pg_restore，逐表比較筆數與全體列資料的雜湊，再把音檔解到臨時目錄逐檔比對 SHA256。通過後產生 `restore-drill.json`，最後移除臨時容器及 volume。演練不連線、不還原到目前正式 DB；壓縮檔路徑穿越、連結或 checksum 不符會拒絕處理。

還原到已建立的 Compose 環境：

```bash
make restore BACKUP_SOURCE=/path/to/bundle
# 只有確定取代現有資料時才使用：
make restore BACKUP_SOURCE=/path/to/bundle RESTORE_REPLACE=1
```

預設拒絕非空 DB／uploads；檢查拒絕時恢復原先服務。真正開始寫入後若還原失敗，應用與 Watchtower 保持停止，避免使用不完整資料。還原採 DB 單一 transaction，但 DB 與音檔之間不是跨系統原子操作。

設定不會自動覆蓋目前 `.env`。在新電腦先將 `configuration.tar.gz` 解到私密的新目錄，檢查並選用其中的 `.env`／部署設定，再建立空白 PostgreSQL 與 app 容器（應用保持停止），執行上述還原。既有環境取代資料前另做備份，不要盲目替換 PostgreSQL 的帳密或 volume 設定。

Kubernetes 新版 bundle 另含 `configuration.json`，保存工作負載、所引用的 ConfigMaps 與 Secrets，並納入 checksum；兩個 app 的副本數在匯出設定中設為 0。只在空白目的環境、初始化 DB 之前套用經檢查的設定，避免把舊帳密套進既有 PostgreSQL。舊版 bundle 沒有這個檔案時，仍須另外保存設定與 Secrets。

本輪已實際驗證 Docker bundle 還原、逐表雜湊一致，以及隔離音檔樣本的內容還原。Kubernetes 的實際叢集演練仍須在你的目的叢集執行；本機驗證不代表已測過兩台電腦的 K3s 傳輸。
