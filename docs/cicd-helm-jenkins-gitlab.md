# CI/CD cho OpenMetadata: build trên WSL, Helm, Jenkins, GitLab CI

Guide cho fork `nqt1310/OpenMetadata` (có connector custom như Excel Report, File Log). Mục tiêu:
build **image server** + **image ingestion** từ source của fork, push lên registry, rồi deploy
bằng **Helm chart chính thức** (`open-metadata/openmetadata-helm-charts`), điều khiển bởi Jenkins
hoặc GitLab CI.

```
git push ──► CI (Jenkins | GitLab)
              ├─ 1. mvn package          → openmetadata-dist/target/openmetadata-*.tar.gz
              ├─ 2. docker build server   → <registry>/openmetadata-server:<tag>
              ├─ 3. docker build ingestion→ <registry>/openmetadata-ingestion:<tag>
              ├─ 4. docker push
              └─ 5. helm upgrade --install (dependencies + openmetadata) --set image.tag=<tag>
```

## 1. Pull nhánh mới nhất và build trên WSL (máy local)

### 1.1 Chuẩn bị WSL (Ubuntu 22.04/24.04)

- Cấp tài nguyên cho WSL trong `%UserProfile%\.wslconfig` (Windows), rồi `wsl --shutdown`:
  ```ini
  [wsl2]
  memory=12GB
  processors=6
  swap=4GB
  ```
- Clone repo **trong filesystem Linux** (`~/src`), **không** ở `/mnt/c/...` — build Maven/Yarn trên
  `/mnt/c` chậm hơn nhiều lần và hay lỗi quyền file.
- Docker: dùng Docker Desktop với *WSL integration* bật cho distro, hoặc cài `docker-ce` trong WSL.
- Tool chain:
  ```bash
  sudo apt update && sudo apt install -y openjdk-21-jdk maven python3.11 python3.11-venv python3.11-dev \
    build-essential pkg-config libkrb5-dev libsasl2-dev unixodbc-dev libpq-dev \
    default-libmysqlclient-dev librdkafka-dev libssl-dev libffi-dev jq
  # thiếu libkrb5-dev => `make install_dev_env` fail "krb5-config: not found" (xem docs/build-logs/2026-09-25/BUILD-NOTES.md)
  # Node 22 + yarn (qua nvm)
  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
  source ~/.bashrc && nvm install 22 && npm i -g yarn
  java -version   # 21
  mvn -v          # >= 3.9
  ```

### 1.2 Tìm và checkout nhánh mới nhất

```bash
cd ~/src
git clone https://github.com/nqt1310/OpenMetadata.git   # lần đầu
cd OpenMetadata
git fetch origin --prune

# Liệt kê nhánh remote theo commit mới nhất
git for-each-ref --sort=-committerdate refs/remotes/origin \
  --format='%(committerdate:short) %(refname:short) %(subject)' | head
```

Tại thời điểm viết, nhánh mới nhất là `claude/co-the-lam-nhung-gi-bqjoty` (nền `release 1.13.3`,
thêm log connector + custom properties cho Excel Report):

```bash
git checkout -B claude/co-the-lam-nhung-gi-bqjoty origin/claude/co-the-lam-nhung-gi-bqjoty
git pull origin claude/co-the-lam-nhung-gi-bqjoty
```

### 1.3 Build

```bash
python3.11 -m venv env && source env/bin/activate
make prerequisites
cd ingestion && make install_dev_env && cd ..
make generate                         # sinh Pydantic models từ JSON Schema (bắt buộc sau khi đổi schema)
make yarn_install_cache

# Backend + UI -> openmetadata-dist/target/openmetadata-<ver>.tar.gz
mvn -DskipTests clean package
# Nhanh hơn khi chỉ sửa backend/ingestion:
# mvn -DskipTests -DonlyBackend clean package -pl '!openmetadata-ui'
```

### 1.4 Chạy full stack local bằng Docker

```bash
# -m ui|no-ui  -d mysql|postgresql  -s true = bỏ qua maven (đã build ở trên)
./docker/run_local_docker.sh -m ui -d mysql -s true
```

- UI: <http://localhost:8585> (`admin@open-metadata.org` / `admin`)
- Airflow (ingestion): <http://localhost:8080> (`admin` / `admin`)

Log build thật + lỗi đã gặp: [build-logs/2026-09-25/BUILD-NOTES.md](build-logs/2026-09-25/BUILD-NOTES.md).

Chỉ test connector Python mà không cần cả stack:
```bash
cd ingestion && pytest tests/unit/topology/messaging/test_filelog.py -q
```

Lỗi hay gặp trên WSL:

| Triệu chứng | Nguyên nhân / cách xử lý |
|---|---|
| Elasticsearch/OpenSearch exit 78 | `sudo sysctl -w vm.max_map_count=262144` (thêm vào `/etc/sysctl.conf`) |
| Maven/Yarn bị kill | WSL thiếu RAM — tăng `memory` trong `.wslconfig` |
| `EACCES` / build rất chậm | Repo nằm ở `/mnt/c` — clone lại vào `~/src` |
| `make generate` lỗi `datamodel-codegen` | Chưa `source env/bin/activate` hoặc chưa `make install_dev_env` |
| Line ending `\r` trong `.sh` | `git config --global core.autocrlf input` rồi clone lại |

## 2. Docker images

Hai image cần build từ fork (các image còn lại dùng upstream):

| Image | Dockerfile | Context | Input |
|---|---|---|---|
| `openmetadata-server` | `docker/development/Dockerfile` | repo root | `openmetadata-dist/target/openmetadata-*.tar.gz` |
| `openmetadata-ingestion` | `ingestion/Dockerfile.ci` | repo root | source `ingestion/` (chứa connector custom) |

```bash
REG=registry.example.com/data
TAG=$(git rev-parse --short HEAD)
docker build -f docker/development/Dockerfile -t $REG/openmetadata-server:$TAG .
docker build -f ingestion/Dockerfile.ci        -t $REG/openmetadata-ingestion:$TAG .
docker push $REG/openmetadata-server:$TAG
docker push $REG/openmetadata-ingestion:$TAG
```

Tag theo commit SHA (immutable) — không deploy `latest`, để rollback bằng `helm rollback` hoặc
đổi tag là đủ.

## 3. Helm

### 3.1 Cấu trúc thư mục đề xuất (trong repo này hoặc repo deploy riêng)

```
deploy/helm/
├── values-dependencies.yaml     # MySQL/OpenSearch/Airflow (chart openmetadata-dependencies)
├── values-common.yaml           # chung cho mọi môi trường
├── values-dev.yaml
├── values-staging.yaml
└── values-prod.yaml
```

### 3.2 Secrets (tạo một lần mỗi namespace, không commit)

```bash
kubectl create ns openmetadata-dev
kubectl -n openmetadata-dev create secret generic mysql-secrets \
  --from-literal=openmetadata-mysql-password='<db-pass>'
kubectl -n openmetadata-dev create secret generic airflow-secrets \
  --from-literal=openmetadata-airflow-password='<airflow-pass>'
kubectl -n openmetadata-dev create secret generic airflow-mysql-secrets \
  --from-literal=airflow-mysql-password='<airflow-db-pass>'
# Registry private
kubectl -n openmetadata-dev create secret docker-registry regcred \
  --docker-server=registry.example.com --docker-username=<u> --docker-password=<p>
```

Prod: dùng External Secrets / Sealed Secrets / Vault thay vì `kubectl create` tay.

### 3.3 `values-dependencies.yaml` — Airflow dùng image ingestion của fork

```yaml
airflow:
  airflow:
    image:
      repository: registry.example.com/data/openmetadata-ingestion
      tag: "REPLACED_BY_CI"
      pullPolicy: IfNotPresent
    config:
      AIRFLOW__OPENMETADATA_AIRFLOW_APIS__DAG_GENERATED_CONFIGS: "/opt/airflow/dag_generated_configs"
  # imagePullSecrets nếu registry private
mysql:
  enabled: true        # prod: false + DB managed (RDS/CloudSQL)
opensearch:
  enabled: true
```

### 3.4 `values-common.yaml` — server

```yaml
image:
  repository: registry.example.com/data/openmetadata-server
  tag: "REPLACED_BY_CI"
  pullPolicy: IfNotPresent
imagePullSecrets:
  - name: regcred
resources:
  requests: { cpu: "1", memory: 2Gi }
  limits:   { memory: 4Gi }
openmetadata:
  config:
    pipelineServiceClientConfig:
      apiEndpoint: http://openmetadata-dependencies-web:8080
```

`values-prod.yaml` chỉ override khác biệt: `replicaCount`, ingress/TLS, DB/search host ngoài,
`authentication`/`authorizer` (SSO).

### 3.5 Lệnh deploy (CI chạy đúng các lệnh này)

```bash
helm repo add open-metadata https://helm.open-metadata.org && helm repo update
NS=openmetadata-dev; TAG=<sha>; CHART_VER=1.13.3   # khớp version server

helm upgrade --install openmetadata-dependencies open-metadata/openmetadata-dependencies \
  -n $NS --version $CHART_VER -f deploy/helm/values-dependencies.yaml \
  --set airflow.airflow.image.tag=$TAG --wait --timeout 15m

helm upgrade --install openmetadata open-metadata/openmetadata \
  -n $NS --version $CHART_VER \
  -f deploy/helm/values-common.yaml -f deploy/helm/values-dev.yaml \
  --set image.tag=$TAG --atomic --wait --timeout 15m

kubectl -n $NS rollout status deploy/openmetadata
```

- `--atomic` tự rollback nếu pod không Ready (migration lỗi, config sai).
- Pin `--version` của chart = version server trong `pom.xml`; đổi chart major phải đọc release notes.
- Migration DB chạy trong init container của chart khi server khởi động — **backup DB trước** khi
  deploy prod.
- Rollback: `helm -n $NS history openmetadata` → `helm -n $NS rollback openmetadata <rev>`.
  Migration không tự rollback, cần restore DB nếu schema đã đổi.

Kiểm tra trước khi merge: `helm template ... | kubeconform -strict -` hoặc `helm upgrade --dry-run`.

## 4. Jenkins

Yêu cầu: agent Linux có Docker (hoặc Kubernetes agent + Kaniko), credentials:
`registry-creds` (username/password), `kubeconfig-dev`, `kubeconfig-prod` (secret file).

`Jenkinsfile` ở repo root:

```groovy
pipeline {
  agent { label 'docker' }
  options {
    timestamps()
    timeout(time: 90, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '20'))
    disableConcurrentBuilds()
  }
  environment {
    REGISTRY  = 'registry.example.com/data'
    TAG       = "${env.GIT_COMMIT.take(8)}"
    CHART_VER = '1.13.3'
    MAVEN_OPTS = '-Xmx4g'
  }
  stages {
    stage('Test ingestion') {
      agent { docker { image 'python:3.10'; reuseNode true } }
      steps {
        sh '''
          python -m venv env && . env/bin/activate
          make generate
          cd ingestion && pip install -e ".[test]" && pytest tests/unit -q -x
        '''
      }
    }
    stage('Build dist') {
      agent {
        docker {
          image 'maven:3.9-eclipse-temurin-21'
          args '-v $HOME/.m2:/root/.m2'
          reuseNode true
        }
      }
      steps { sh 'mvn -B -DskipTests clean package' }
    }
    stage('Docker build & push') {
      steps {
        withCredentials([usernamePassword(credentialsId: 'registry-creds',
                         usernameVariable: 'U', passwordVariable: 'P')]) {
          sh '''
            echo "$P" | docker login registry.example.com -u "$U" --password-stdin
            docker build -f docker/development/Dockerfile -t $REGISTRY/openmetadata-server:$TAG .
            docker build -f ingestion/Dockerfile.ci        -t $REGISTRY/openmetadata-ingestion:$TAG .
            docker push $REGISTRY/openmetadata-server:$TAG
            docker push $REGISTRY/openmetadata-ingestion:$TAG
          '''
        }
      }
    }
    stage('Deploy dev') {
      when { branch 'main' }
      steps { deploy('openmetadata-dev', 'dev', 'kubeconfig-dev') }
    }
    stage('Deploy prod') {
      when { buildingTag() }
      steps {
        input message: "Deploy ${TAG} lên PROD?", ok: 'Deploy'
        deploy('openmetadata', 'prod', 'kubeconfig-prod')
      }
    }
  }
  post { always { sh 'docker logout registry.example.com || true' } }
}

def deploy(String ns, String envName, String kubeCred) {
  withCredentials([file(credentialsId: kubeCred, variable: 'KUBECONFIG')]) {
    sh """
      helm repo add open-metadata https://helm.open-metadata.org && helm repo update
      helm upgrade --install openmetadata-dependencies open-metadata/openmetadata-dependencies \
        -n ${ns} --version ${CHART_VER} -f deploy/helm/values-dependencies.yaml \
        --set airflow.airflow.image.tag=${TAG} --wait --timeout 15m
      helm upgrade --install openmetadata open-metadata/openmetadata \
        -n ${ns} --version ${CHART_VER} \
        -f deploy/helm/values-common.yaml -f deploy/helm/values-${envName}.yaml \
        --set image.tag=${TAG} --atomic --wait --timeout 15m
    """
  }
}
```

Ghi chú:
- Dùng **Multibranch Pipeline** để mỗi nhánh/PR tự chạy stage test + build; chỉ `main` deploy dev,
  chỉ git tag deploy prod (có bước `input` duyệt tay).
- Cache `~/.m2` và Yarn cache trên agent — build UI + backend lạnh mất 20–40 phút.
- Agent chạy trong K8s không có Docker daemon: thay stage Docker bằng container
  `gcr.io/kaniko-project/executor` (`--dockerfile`, `--context`, `--destination`).

## 5. GitLab CI

Biến CI/CD (Settings → CI/CD → Variables, *Masked + Protected*): `KUBECONFIG_DEV`,
`KUBECONFIG_PROD` (kiểu **File**). Registry dùng GitLab Container Registry sẵn có
(`$CI_REGISTRY_IMAGE`, `$CI_REGISTRY_USER`, `$CI_REGISTRY_PASSWORD`).

`.gitlab-ci.yml`:

```yaml
stages: [test, build, package, deploy]

variables:
  TAG: $CI_COMMIT_SHORT_SHA
  CHART_VER: "1.13.3"
  MAVEN_OPTS: "-Xmx4g -Dmaven.repo.local=$CI_PROJECT_DIR/.m2"

default:
  interruptible: true

test:ingestion:
  stage: test
  image: python:3.10
  cache: { key: pip, paths: [.cache/pip] }
  variables: { PIP_CACHE_DIR: "$CI_PROJECT_DIR/.cache/pip" }
  script:
    - python -m venv env && . env/bin/activate
    - make generate
    - cd ingestion && pip install -e ".[test]" && pytest tests/unit -q -x

build:dist:
  stage: build
  image: maven:3.9-eclipse-temurin-21
  cache: { key: m2, paths: [.m2] }
  script:
    - mvn -B -DskipTests clean package
  artifacts:
    paths: [openmetadata-dist/target/openmetadata-*.tar.gz]
    expire_in: 1 day

.kaniko:
  stage: package
  image: { name: gcr.io/kaniko-project/executor:v1.23.2-debug, entrypoint: [""] }
  before_script:
    - mkdir -p /kaniko/.docker
    - >
      echo "{\"auths\":{\"$CI_REGISTRY\":{\"auth\":\"$(printf '%s:%s' "$CI_REGISTRY_USER" "$CI_REGISTRY_PASSWORD" | base64 | tr -d '\n')\"}}}"
      > /kaniko/.docker/config.json

package:server:
  extends: .kaniko
  needs: [build:dist]
  script:
    - /kaniko/executor --context $CI_PROJECT_DIR
        --dockerfile docker/development/Dockerfile
        --destination $CI_REGISTRY_IMAGE/openmetadata-server:$TAG
        --cache=true

package:ingestion:
  extends: .kaniko
  needs: [test:ingestion]
  script:
    - /kaniko/executor --context $CI_PROJECT_DIR
        --dockerfile ingestion/Dockerfile.ci
        --destination $CI_REGISTRY_IMAGE/openmetadata-ingestion:$TAG
        --cache=true

.deploy:
  stage: deploy
  image: { name: alpine/helm:3.16.2, entrypoint: [""] }
  needs: [package:server, package:ingestion]
  script:
    - helm repo add open-metadata https://helm.open-metadata.org && helm repo update
    - helm upgrade --install openmetadata-dependencies open-metadata/openmetadata-dependencies
        -n $NS --version $CHART_VER -f deploy/helm/values-dependencies.yaml
        --set airflow.airflow.image.repository=$CI_REGISTRY_IMAGE/openmetadata-ingestion
        --set airflow.airflow.image.tag=$TAG --wait --timeout 15m
    - helm upgrade --install openmetadata open-metadata/openmetadata
        -n $NS --version $CHART_VER
        -f deploy/helm/values-common.yaml -f deploy/helm/values-$ENV_NAME.yaml
        --set image.repository=$CI_REGISTRY_IMAGE/openmetadata-server
        --set image.tag=$TAG --atomic --wait --timeout 15m

deploy:dev:
  extends: .deploy
  variables: { NS: openmetadata-dev, ENV_NAME: dev, KUBECONFIG: $KUBECONFIG_DEV }
  environment: { name: dev }
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH

deploy:prod:
  extends: .deploy
  variables: { NS: openmetadata, ENV_NAME: prod, KUBECONFIG: $KUBECONFIG_PROD }
  environment: { name: production }
  resource_group: production
  rules:
    - if: $CI_COMMIT_TAG
      when: manual
```

Ghi chú:
- Kaniko không cần Docker daemon/privileged — phù hợp GitLab Runner Kubernetes executor.
  Runner Docker executor có thể dùng `docker:dind` thay thế.
- `resource_group: production` chặn hai deploy prod chạy song song.
- Bật *Protected environments* cho `production` để chỉ Maintainer bấm được job manual.
- Thay kubeconfig bằng **GitLab Agent for Kubernetes** (`KUBE_CONTEXT`) nếu cluster không cho
  truy cập API từ ngoài.

## 6. Checklist vận hành

- [ ] Tag image = commit SHA; version chart pin khớp version server.
- [ ] Không có secret trong repo/values; secrets qua K8s Secret / External Secrets / biến masked.
- [ ] Backup DB + snapshot index search trước deploy prod (migration không rollback được).
- [ ] `vm.max_map_count=262144` trên node chạy OpenSearch/Elasticsearch.
- [ ] Sau khi đổi JSON Schema: `make generate` trong pipeline trước khi test/build ingestion.
- [ ] Sau deploy: `kubectl rollout status`, mở UI, chạy thử một ingestion pipeline của connector custom.
- [ ] Đồng bộ upstream định kỳ: `git remote add upstream https://github.com/open-metadata/OpenMetadata.git`
      rồi merge nhánh release tương ứng vào fork.
