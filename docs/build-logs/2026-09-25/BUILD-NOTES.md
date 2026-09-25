# Build log & lưu ý — nhánh `claude/co-the-lam-nhung-gi-bqjoty` (2026-09-25)

Build thử bằng `scripts/wsl/build-with-logs.sh` trên môi trường tương đương WSL
(Ubuntu 24.04.4, Java 21.0.10, Maven 3.9.11, Node 22.22.2, Yarn 1.22.22, Python 3.11.15,
Docker 29.3.1, 4 CPU, 15 GB RAM, **không swap**). Commit `694809c253`.

> Build **dừng giữa chừng theo yêu cầu** (ở bước `yarn install`); các bước Maven, unit test,
> Docker image **chưa chạy**. Kết quả dưới đây chỉ phản ánh những gì đã chạy thật.

## Kết quả từng bước

| Bước | Lệnh | Kết quả | Thời gian | Log |
|---|---|---|---|---|
| env | thu thập version tool | OK | 1s | `run1-full/env.log.txt` |
| venv | `python3.11 -m venv env` | OK | 3s | `run1-full/venv.log.txt` |
| prerequisites | `make prerequisites` | OK | 1s | `run1-full/prerequisites.log.txt` |
| install_dev_env | `make -C ingestion install_dev_env` | **FAIL** — wheel `kerberos` | 5m38s | `run1-full/install_dev_env.log.txt` |
| generate | `make generate` | **FAIL** (hệ quả của bước trên) | 0s | `run1-full/generate.log.txt` |
| *(sửa)* | `apt-get install libkrb5-dev ...` | OK | — | `run1-full/apt-fix.log.txt` |
| install_dev_env (lần 2) | như trên | `kerberos` build **OK**; dừng tay khi đang cài | — | `run2-python-retry/install_dev_env.log.txt` |
| yarn_install | `make yarn_install_cache` | dừng tay ở `[3/5] Fetching packages` | — | `run1-full/yarn_install.log.txt` |
| maven / unit_tests / docker_* | — | chưa chạy | — | — |

## Lỗi gặp phải và cách xử lý

### 1. `Failed building wheel for kerberos` — `krb5-config: not found`

```
building 'kerberos' extension
x86_64-linux-gnu-gcc ... /bin/sh: 1: krb5-config: not found
ERROR: Failed building wheel for kerberos
make: *** [Makefile:21: install_dev_env] Error 1
```

- Nguyên nhân: extra `all-dev-env` kéo `impyla[kerberos]` → `kerberos` phải compile C, cần header Kerberos.
- Sửa (đã xác nhận ở run2: `Successfully built openmetadata-ingestion kerberos`):
  ```bash
  sudo apt-get install -y libkrb5-dev libsasl2-dev unixodbc-dev libpq-dev \
    default-libmysqlclient-dev librdkafka-dev
  ```
  Các package còn lại trong danh sách cũng thiếu trên Ubuntu sạch và sẽ làm fail các wheel khác
  (`sasl`, `pyodbc`, `psycopg2`, `mysqlclient`, `confluent-kafka`) — cài luôn một lần.
- `install_dev_env` mất ~5–6 phút lần đầu (tải > 1 GB wheel); lần sau dùng cache `~/.cache/pip`.

### 2. `make generate` → `ModuleNotFoundError: No module named 'datamodel_code_generator'`

- Không phải lỗi riêng: `datamodel-code-generator` được cài trong `install_dev_env`, bước đó fail nên
  generate fail theo. Luôn kiểm tra `install_dev_env` pass trước khi chạy `make generate`.
- `make generate` **xoá** `ingestion/src/metadata/generated` trước khi sinh lại — nếu fail giữa chừng,
  package `metadata` sẽ import lỗi cho tới khi generate chạy lại thành công.

## Cảnh báo (không làm fail build, nhưng nên biết)

| Cảnh báo | Ý nghĩa / hành động |
|---|---|
| `Makefile:147: warning: overriding recipe for target 'build-ingestion-base-local'` | Makefile gốc định nghĩa target 2 lần (upstream). Bỏ qua. |
| `SetuptoolsDeprecationWarning ... license` khi build wheel | Từ package bên thứ ba. Bỏ qua. |
| Yarn: `Resolution field "X" is incompatible with requested version` (~40 dòng: `@babel/runtime`, `semver`, `prosemirror-*`, `minimatch`) | Do `resolutions` trong `package.json` ép version. Có chủ đích từ upstream, bỏ qua. |
| `vm.max_map_count = 65530` (trong `env.log`) | **Quá thấp cho Elasticsearch/OpenSearch** khi chạy Docker stack — phải nâng lên 262144 (xem dưới). |
| Swap = 0 | Maven + Yarn build UI cần nhiều RAM; không swap dễ bị OOM-kill. Đặt `swap=4GB` trong `.wslconfig`. |

## Điểm phải lưu ý khi build trên WSL

1. **Cài đủ thư viện hệ thống trước** (tránh mất 5 phút rồi fail):
   ```bash
   sudo apt-get install -y openjdk-21-jdk maven python3.11 python3.11-venv python3.11-dev \
     build-essential pkg-config libkrb5-dev libsasl2-dev unixodbc-dev libpq-dev \
     default-libmysqlclient-dev librdkafka-dev libssl-dev libffi-dev jq
   ```
2. **Thứ tự bắt buộc**: `venv` → `make prerequisites` → `make -C ingestion install_dev_env` →
   `make generate` → `make yarn_install_cache` → `mvn -DskipTests clean package`.
   Mỗi lần đổi JSON Schema phải chạy lại `make generate`.
3. **Repo phải nằm trên ext4 của WSL** (`~/src/...`), không ở `/mnt/c` — `docker/excelreport/docker-compose.wsl-isolated.yml`
   đã ghi nhận MySQL không tạo được unix socket trên `/mnt/c` (`Bind on unix socket: Operation not supported`).
4. **`vm.max_map_count`**: trong WSL chạy
   `sudo sysctl -w vm.max_map_count=262144`; để giữ sau reboot thêm vào `/etc/sysctl.conf`
   (hoặc `kernelCommandLine = sysctl.vm.max_map_count=262144` trong `.wslconfig`).
5. **`.wslconfig`**: `memory=12GB`, `processors=6`, `swap=4GB` — môi trường thử 15 GB RAM không swap là mức tối thiểu.
6. **Trùng stack OpenMetadata khác trên cùng Docker engine**: dùng
   `docker compose -p excelreport -f docker/docker-compose-quickstart/docker-compose.yml -f docker/excelreport/docker-compose.wsl-isolated.yml -f docker/excelreport/docker-compose.override.yml ...`
   (tên container/subnet riêng, cổng UI **18585**, Airflow **18080**).
7. **`docker/excelreport/Dockerfile.airflow` đang `BASE_TAG=1.12.14`** trong khi source là **1.13.3** —
   image ingestion lệch version server. Build bằng `--build-arg BASE_TAG=1.13.3` (hoặc sửa default).
   Dockerfile này cũng chỉ copy connector `excelreport`, **không** copy `messaging/filelog`,
   `messaging/logs`, `parsers/logs` — muốn chạy connector log trong Airflow phải dùng
   `ingestion/Dockerfile.ci` (build full source) hoặc bổ sung các `COPY` đó. Đường dẫn cũng hard-code
   `python3.10/site-packages`.
8. **`.gitignore` có pattern `logs` và `*.log`**: package `.../logs/` đã được whitelist trên nhánh;
   thêm package/thư mục nào tên `logs` phải whitelist tương tự, nếu không file sẽ không được commit.
   (Log build trong thư mục này vì thế được lưu đuôi `.log.txt`.)
9. **Line ending**: `git config --global core.autocrlf input` trước khi clone — script `.sh` có `\r` sẽ lỗi
   `bad interpreter`.

## Chạy lại để lấy log đầy đủ

```bash
scripts/wsl/build-with-logs.sh                                   # tất cả bước, dừng ở bước lỗi đầu tiên
CONTINUE_ON_ERROR=true scripts/wsl/build-with-logs.sh            # chạy hết, ghi lỗi vào SUMMARY.txt
scripts/wsl/build-with-logs.sh maven docker_server               # chỉ một số bước
# log: build-logs/<timestamp>/<bước>.log  + SUMMARY.txt (thời gian, rc, 40 dòng cuối khi lỗi)
```
