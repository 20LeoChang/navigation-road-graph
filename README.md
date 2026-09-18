# Taiwan ROAD_GRAPH 全台預建

這套流程直接產生目前 `navigation` Worker 可讀的：

`graph/v1/TW/<south>_<west>.json`

不需要修改 Worker，也不需要再用 `/graph/build-tile` 一格一格抓 OSM Main API。

## 1. 建 GitHub repository

把這個資料夾內容放進一個 GitHub repository：

- `.github/workflows/build-taiwan-road-graph.yml`
- `scripts/build_taiwan_graph.py`
- `scripts/upload_r2.py`
- `requirements.txt`

## 2. Cloudflare R2 建 API Token

Cloudflare Dashboard：

Storage & databases → R2 → Overview → Manage API Tokens

建立只允許 `navigation` bucket 的 Object Read & Write token。

記下：

- Access Key ID
- Secret Access Key
- S3 endpoint

一般 endpoint：

`https://<ACCOUNT_ID>.r2.cloudflarestorage.com`

若 bucket 有 jurisdiction，請直接使用 Cloudflare 顯示的 jurisdiction-specific endpoint。

## 3. GitHub Secrets

GitHub repository：

Settings → Secrets and variables → Actions → New repository secret

建立：

- `R2_ENDPOINT`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`

`R2_ENDPOINT` 要填完整 URL。

## 4. 第一次跑

GitHub → Actions → Build Taiwan ROAD_GRAPH → Run workflow

流程會：

1. 下載 `taiwan-latest.osm.pbf`
2. 一次串流解析全台 OSM
3. 依 0.05° × 0.05° 分 tile
4. 建 directed car graph
5. 產生 `graph/v1/TW/*.json`
6. 上傳到 R2 bucket `navigation`
7. 刪除同 prefix 下舊版已不存在的 tile

## 5. 與目前 Worker 相容

格式與目前 Worker v3.2.0 一致：

Nodes:

`["osmNodeId", lat, lon]`

Edges:

`["from","to",lengthM,defaultSpeedKph,highway,name,osmWayId]`

ownership：

`edge stored in tile containing FROM node`

因此 Worker 原本的：

`ROAD_GRAPH → navigation`

不需要改。

## 6. 更新頻率

Workflow 已設：

每週一 03:30（台灣時間）自動重建一次。

也可以隨時手動 Run workflow。

## 7. v3.2.0 Queue 還是保留

全台預建是正常資料來源。

`GRAPH_BUILD_QUEUE` 則保留成缺檔修復 fallback。正常情況不應該靠 Queue 建整個台灣。

## 注意

目前 pipeline 直接更新 `graph/v1/TW/`。因此上傳的幾分鐘內可能存在少量新舊 tile 混用。

等全台 routing 驗證完成後，再升級成 release/pointer：

`graph/releases/<release>/TW/...`

最後原子切換 `graph/current.json`。

這樣正式更新時就能做到零混版。
