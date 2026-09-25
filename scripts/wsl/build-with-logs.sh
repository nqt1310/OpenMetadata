#!/usr/bin/env bash
#  Copyright 2026 Collate
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  http://www.apache.org/licenses/LICENSE-2.0
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
# Build OpenMetadata step by step, writing one full log per step plus a summary.
# Usage: scripts/wsl/build-with-logs.sh [STEPS...]   (default: all steps)
#   env LOG_DIR=...  where logs go (default: build-logs/<timestamp>)
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
LOG_DIR="${LOG_DIR:-$REPO_ROOT/build-logs/$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$LOG_DIR"
SUMMARY="$LOG_DIR/SUMMARY.txt"

ALL_STEPS=(env venv prerequisites install_dev_env generate yarn_install maven docker_server docker_ingestion unit_tests)
STEPS=("${@:-${ALL_STEPS[@]}}")

step_env() {
  uname -a; cat /etc/os-release 2>/dev/null | head -2
  git rev-parse --abbrev-ref HEAD; git log -1 --oneline
  java -version 2>&1; mvn -v 2>&1 | head -3
  node -v; yarn -v; python3.11 --version
  docker --version || true
  nproc; free -h; df -h "$REPO_ROOT"
  sysctl vm.max_map_count || true
}
step_venv()            { [[ -d env ]] || python3.11 -m venv env; }
step_prerequisites()   { . env/bin/activate && make prerequisites; }
step_install_dev_env() { . env/bin/activate && make -C ingestion install_dev_env; }
step_generate()        { . env/bin/activate && make generate; }
step_yarn_install()    { make yarn_install_cache; }
step_maven()           { mvn -B -DskipTests clean package; }
step_docker_server()   { docker build -f docker/development/Dockerfile -t openmetadata-server:local .; }
step_docker_ingestion(){ docker build -f ingestion/Dockerfile.ci -t openmetadata-ingestion:local .; }
step_unit_tests()      { . env/bin/activate && cd ingestion && python -m pytest tests/unit -q -x -p no:cacheprovider; }

echo "Build started $(date -Is) on $(git rev-parse --abbrev-ref HEAD)@$(git rev-parse --short HEAD)" | tee "$SUMMARY"
for s in "${STEPS[@]}"; do
  log="$LOG_DIR/$s.log"
  start=$(date +%s)
  echo ">>> [$s] started $(date -Is)" | tee -a "$SUMMARY"
  ( set -x; "step_$s" ) >"$log" 2>&1
  rc=$?
  dur=$(( $(date +%s) - start ))
  printf '<<< [%s] rc=%d duration=%dm%02ds log=%s\n' "$s" "$rc" $((dur/60)) $((dur%60)) "$log" | tee -a "$SUMMARY"
  if [[ $rc -ne 0 ]]; then
    echo "--- last 40 lines of $log ---" | tee -a "$SUMMARY"
    tail -40 "$log" | tee -a "$SUMMARY"
    [[ "${CONTINUE_ON_ERROR:-false}" == "true" ]] || exit $rc
  fi
done
echo "Build finished $(date -Is)" | tee -a "$SUMMARY"
