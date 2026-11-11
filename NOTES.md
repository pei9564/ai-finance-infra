# notes — 架構筆記與決策文件

這個分支放 `ai-finance-infra` 的架構敘事、ADR 與互動 demo。

`main` 只保留程式、部署設定與**操作文件**（安裝、遷移、備份還原）。
這裡的東西沒有在持續經營，所以從 `main` 的 `docs/` 切出來單獨放，避免
repo 首頁給人「有一份維護中的文件站」的錯誤期待。

## 內容

| 檔案 | 內容 |
|---|---|
| `docs/index.html` | Landing：硬體基線表、系統架構圖、ADR 卡片 |
| `docs/build-log.md` | 工程決策日誌（逐日） |
| `docs/viewer.html` | Markdown 瀏覽器（`?file=adr/adr-001-k3s-selection.md`） |
| `docs/adr/` | ADR-001 編排器選型 · ADR-005 本地推論 runtime · ADR-006 環境隔離 |
| `docs/demos/four-node-topology.html` | 互動 demo：4 節點角色分派 |
| `docs/demos/serving-runtime-decision.html` | 互動 demo：VRAM 模擬器與 runtime 選型 |

## 本機預覽

純靜態，無 build step：

```bash
python3 -m http.server -d docs 8000
# → http://localhost:8000/
```

## 狀態

- ADR-001 / ADR-005 / ADR-006 的 Status 仍是 Proposed，對應的 K3s 部署尚未實跑驗證。
- 互動 demo 與 Landing 的硬體規格已做去識別處理（不列型號、年份、精確容量），
  但保留決策所依據的約束（單張可用 GPU、單顆旋轉碟、RAM 最大的節點）。

## 操作文件在哪

在 `main` 分支：

- `docs/k3s-tailscale-setup.md` — K3s + Tailscale 兩節點安裝
- `docs/k3s-setup-draft.md` — Compose → K3s 資料遷移
- `docs/backup-and-restore.md` — 備份與還原
