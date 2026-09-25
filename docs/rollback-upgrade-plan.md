# Upgrade plan & Rollback plan — OpenMetadata fork `nqt1310/OpenMetadata`

Áp dụng cho deployment chạy bằng Docker Compose (WSL/local) và Helm (K8s) như mô tả trong
[cicd-helm-jenkins-gitlab.md](cicd-helm-jenkins-gitlab.md).

## 0. Hiện trạng

| Thành phần | Giá trị |
|---|---|
| Nhánh fork mới nhất | `claude/co-the-lam-nhung-gi-bqjoty` |
| Nền upstream | `release 1.13.3` (commit `255f669491`) |
| Thay đổi riêng của fork | **chỉ thêm mới** — connector Python `dashboard/excelreport`, `messaging/filelog`, `messaging/logs`, `parsers/logs`, `docker/excelreport/*`, 4 dòng trong `ingestion/Dockerfile.ci` (`pip install openpyxl`), `.gitignore` |
| Không đụng | Java backend, JSON Schema, SQL migration, UI |
| Migration upstream kế tiếp | `1.13.4` → `2.0.0` → `2.1.0` (`bootstrap/sql/migrations/native/`) |

Hệ quả:
- Fork **không có migration riêng** ⇒ schema DB của fork = schema upstream cùng version. Upgrade/rollback
  DB theo đúng quy trình upstream.
- Connector custom chạy dạng `sourcePythonClass` (Custom Dashboard / Custom Messaging) ⇒ chỉ phụ thuộc
  API Python `metadata.*` của ingestion. Rủi ro chính khi upgrade là **API Python/Pydantic model thay đổi**,
  không phải conflict merge.

## 1. Nguyên tắc (đọc trước khi làm bất kỳ upgrade/rollback nào)

1. **Migration DB là một chiều.** `openmetadata-ops.sh migrate` không có "down". Rollback version server
   sau khi migration đã chạy = **restore DB từ backup**. Không có backup ⇒ không có rollback.
2. **Server, ingestion image và Python client phải cùng version** (`major.minor.patch`). Ingestion kiểm tra
   version server khi kết nối; lệch version ⇒ pipeline fail hoặc hành vi sai. Nâng/hạ **cả bộ**.
3. **Search index là dữ liệu dẫn xuất.** Không cần backup bắt buộc — có thể dựng lại bằng reindex
   (`Search Indexing` app, `recreateIndex=true`). Snapshot chỉ để rút ngắn thời gian rollback.
4. **Không nhảy cóc major nếu release notes yêu cầu đi qua bản trung gian.** Từ 1.13.x lên 2.x: lên
   `1.13.<patch mới nhất>` trước, xác minh, rồi mới lên `2.0.x`.
5. **Mọi thay đổi đi qua dev → staging → prod**, cùng một image tag (commit SHA), không build lại cho prod.

## 2. Upgrade plan

### 2.1 Lộ trình

| Bước | Từ → Đến | Loại | Ghi chú |
|---|---|---|---|
| U1 | 1.13.3 → 1.13.4 | patch | có migration `1.13.4`; rủi ro thấp |
| U2 | 1.13.4 → 2.0.x | **major** | migration `2.0.0` có `ALTER/DROP`; đọc release notes + breaking changes; test kỹ connector custom |
| U3 | 2.0.x → 2.1.x | minor | migration `2.1.0` |

Mỗi bước là một vòng đầy đủ 2.2 → 2.6 dưới đây.

### 2.2 Chuẩn bị code (trên máy dev/WSL)

```bash
git remote add upstream https://github.com/open-metadata/OpenMetadata.git   # một lần
git fetch upstream --tags
git checkout -b upgrade/1.13.4 origin/claude/co-the-lam-nhung-gi-bqjoty
git merge upstream/<release-branch-hoặc-tag 1.13.4>        # merge, không rebase: giữ lịch sử fork
```

- Conflict dự kiến: chỉ `ingestion/Dockerfile.ci` và `.gitignore` (các file fork có sửa). Giữ phần
  `pip install openpyxl` và các dòng `!ingestion/src/metadata/.../logs/`.
- `make generate` (models sinh lại theo schema mới) → chạy unit test connector custom:
  ```bash
  cd ingestion && pytest -q tests/unit/topology/dashboard/test_excelreport_parser.py \
    tests/unit/topology/dashboard/test_excelreport_custom_properties.py \
    tests/unit/topology/messaging/test_filelog.py tests/unit/test_log_parser.py
  ```
- Với U2 (major): grep các import mà connector custom dùng từ `metadata.*` và đối chiếu với upstream mới
  (class/method bị đổi tên, model Pydantic đổi field). Sửa connector trong cùng nhánh upgrade.
- Cập nhật `docker/excelreport/Dockerfile.airflow`: `BASE_TAG` phải = version mới (hiện đang ghi
  `1.12.14`, **lệch** với nền 1.13.3 — sửa ngay cả khi chưa upgrade).
- Pin version chart Helm (`CHART_VER`) = version mới trong pipeline.

### 2.3 Pre-flight (trước cửa sổ bảo trì, trên môi trường đích)

- [ ] Image server + ingestion của version mới đã build, push, chạy OK trên **dev** và **staging**.
- [ ] Staging được restore từ **bản backup prod gần nhất** rồi chạy upgrade thử → đo thời gian migration
      và reindex (dùng để ước lượng downtime prod).
- [ ] Kiểm tra dung lượng đĩa DB ≥ 2× kích thước DB hiện tại (migration có thể tạo bảng tạm).
- [ ] Ghi lại version hiện tại:
      `./bootstrap/openmetadata-ops.sh info` (Docker: `docker exec <server> ./bootstrap/openmetadata-ops.sh info`)
      và `helm -n <ns> history openmetadata`.
- [ ] Thông báo downtime; tạm dừng lịch ingestion (pause DAG trong Airflow) để không có pipeline ghi
      dữ liệu trong lúc migration.

### 2.4 Backup (bắt buộc, ngay trước upgrade)

```bash
TS=$(date +%Y%m%d-%H%M)
# MySQL
mysqldump -h <host> -u <user> -p --single-transaction --routines --triggers \
  --databases openmetadata_db airflow_db > om-backup-$TS.sql
# PostgreSQL
pg_dump -h <host> -U <user> -Fc openmetadata_db > om-backup-$TS.dump
pg_dump -h <host> -U <user> -Fc airflow_db     > airflow-backup-$TS.dump
# (tuỳ chọn) snapshot OpenSearch/Elasticsearch
curl -XPUT "http://<search>:9200/_snapshot/<repo>/om-$TS?wait_for_completion=true"
```

- Docker Compose/WSL: dừng stack rồi copy thêm volume (`docker run --rm -v <vol>:/v -v $PWD:/b alpine tar czf /b/vol-$TS.tgz -C /v .`).
- Managed DB (RDS/CloudSQL): tạo snapshot thủ công.
- **Kiểm tra backup restore được** (restore thử vào DB tạm) — backup chưa kiểm tra coi như không có.

### 2.5 Thực hiện

**Helm (K8s):**
```bash
NS=openmetadata; TAG=<sha-mới>; CHART_VER=<version-mới>
helm upgrade --install openmetadata-dependencies open-metadata/openmetadata-dependencies \
  -n $NS --version $CHART_VER -f deploy/helm/values-dependencies.yaml \
  --set airflow.airflow.image.tag=$TAG --wait --timeout 20m
helm upgrade openmetadata open-metadata/openmetadata -n $NS --version $CHART_VER \
  -f deploy/helm/values-common.yaml -f deploy/helm/values-prod.yaml \
  --set image.tag=$TAG --wait --timeout 30m          # migration chạy trong init container
kubectl -n $NS logs deploy/openmetadata -c run-db-migrations --tail=200   # tên container tuỳ chart
```
Không dùng `--atomic` cho bước upgrade prod có migration: auto-rollback chart sẽ đưa image cũ lên
**DB đã migrate** ⇒ trạng thái không nhất quán. Để fail thì dừng lại và chạy Rollback plan có kiểm soát.

**Docker Compose / WSL:**
```bash
docker compose -p excelreport -f ... down                      # giữ volume
export OPENMETADATA_SERVER_IMAGE=<server:new> OPENMETADATA_INGESTION_IMAGE=<ingestion:new>
docker compose -p excelreport -f ... up -d mysql elasticsearch
docker compose -p excelreport -f ... up execute-migrate-all    # chạy foreground, xem log migration
docker compose -p excelreport -f ... up -d openmetadata-server ingestion
```

### 2.6 Post-upgrade

- [ ] `openmetadata-ops.sh info` hiển thị version mới, `SERVER_CHANGE_LOG` có đủ các version migration mới.
- [ ] Settings → Applications → **Search Indexing**: chạy với *Recreate Indexes* (bắt buộc sau major).
- [ ] Redeploy pipelines: `./bootstrap/openmetadata-ops.sh deploy-pipelines` (DAG trong Airflow được sinh
      lại theo version mới).
- [ ] Bật lại lịch ingestion; chạy tay 1 lần pipeline **Excel Report** và **File Log**, kiểm tra
      entity + custom properties xuất hiện đúng.
- [ ] Smoke test UI: login/SSO, search, lineage, glossary, data quality.
- [ ] Theo dõi log server/ingestion 24h; giữ backup ít nhất 14 ngày.

## 3. Rollback plan

### 3.1 Điểm quyết định rollback

Rollback khi **bất kỳ** điều sau xảy ra và không khắc phục được trong cửa sổ bảo trì (đặt trước, ví dụ 60 phút):
- Migration fail / server không lên được (`CrashLoopBackOff`, health check `:8586/healthcheck` đỏ).
- Mất dữ liệu hoặc lỗi chức năng nghiêm trọng khi smoke test.
- Connector custom không chạy được và business không chấp nhận chờ bản sửa.

Ghi nhận: thời điểm, version, log lỗi (`kubectl logs` / `docker logs`) **trước khi** rollback.

### 3.2 Ma trận rollback

| Tình huống | Cách rollback | Cần restore DB? |
|---|---|---|
| A. Chỉ đổi image, **không có migration mới** (vd. sửa connector, cùng version) | `helm rollback` hoặc đổi lại image tag | Không |
| B. Migration **chưa chạy** (fail ở pull image, init container trước migrate) | `helm rollback` | Không |
| C. Migration **đã chạy** (một phần hoặc toàn bộ) | Dừng server → **restore DB** → deploy lại version cũ → reindex | **Có** |
| D. Chỉ ingestion/connector lỗi, server OK | Rollback riêng image ingestion (Airflow) về tag cũ **cùng version server** | Không |

Kiểm tra migration đã chạy chưa: `openmetadata-ops.sh info` hoặc bảng `SERVER_CHANGE_LOG`
(`SELECT version, installed_on FROM SERVER_CHANGE_LOG ORDER BY installed_rank DESC LIMIT 5;` —
version mới xuất hiện ⇒ migration đã chạy).

### 3.3 Quy trình — tình huống A/B/D

```bash
helm -n $NS history openmetadata
helm -n $NS rollback openmetadata <revision-trước>
helm -n $NS rollback openmetadata-dependencies <revision-trước>   # nếu đã đổi image ingestion
kubectl -n $NS rollout status deploy/openmetadata
```
Jenkins/GitLab: chạy lại job deploy của pipeline cũ (cùng commit SHA cũ) cho kết quả tương đương.

### 3.4 Quy trình — tình huống C (có migration)

1. Scale server về 0 và pause Airflow DAG:
   `kubectl -n $NS scale deploy/openmetadata --replicas=0`
2. Restore DB:
   ```bash
   # MySQL
   mysql -h <host> -u <user> -p -e "DROP DATABASE openmetadata_db; CREATE DATABASE openmetadata_db;"
   mysql -h <host> -u <user> -p openmetadata_db < om-backup-<TS>.sql
   # PostgreSQL
   pg_restore -h <host> -U <user> --clean --if-exists -d openmetadata_db om-backup-<TS>.dump
   ```
   (Managed DB: restore snapshot ra instance mới rồi trỏ `DB_HOST` sang.) Restore luôn `airflow_db`
   nếu upgrade đã đổi image Airflow.
3. Deploy lại version cũ: `helm -n $NS rollback openmetadata <rev>` (và `openmetadata-dependencies`).
   Migration của version cũ sẽ thấy DB đúng version ⇒ không chạy gì.
4. Search: restore snapshot, **hoặc** xoá index và chạy Search Indexing *Recreate Indexes*
   (mapping version mới không tương thích ngược).
5. `deploy-pipelines`, bật lại DAG, smoke test như 2.6.
6. Dữ liệu ghi vào hệ thống **sau thời điểm backup** sẽ mất ⇒ đó là lý do phải pause ingestion ở 2.3.

**Docker Compose / WSL:** `down` → xoá volume DB (`docker volume rm excelreport_excelreport-mysql-data`)
→ tạo lại và import backup (hoặc giải nén `vol-<TS>.tgz`) → `up` với image cũ.

### 3.5 Sau rollback

- Post-mortem: nguyên nhân, sửa trên nhánh `upgrade/*`, chạy lại staging (restore từ backup prod mới).
- Không thử lại upgrade prod khi chưa tái hiện và qua được lỗi trên staging.

## 4. RACI / checklist thời gian (mẫu cho cửa sổ bảo trì)

| T | Việc | Người |
|---|---|---|
| T-7d | Upgrade staging thành công, đo thời gian | Dev |
| T-1d | Thông báo downtime, chốt image tag | Lead |
| T-30m | Pause ingestion, backup DB (+ verify), snapshot search | Ops |
| T0 | Deploy version mới, theo dõi migration | Ops |
| T+30m | Reindex, deploy-pipelines, smoke test | Dev + Ops |
| T+60m | **Go / No-go**: không đạt ⇒ Rollback plan 3.x | Lead |
| T+24h | Đóng sự kiện, giữ backup 14 ngày | Ops |
