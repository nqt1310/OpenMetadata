# Standalone image to run the Excel Report ("YCPT_BAOCAO") Custom Dashboard
# connector (ingestion/src/metadata/ingestion/source/dashboard/excelreport)
# without needing a full local Python/WSL dev environment.
#
# Build from the repo root, since the schema generation step needs
# openmetadata-spec/ and scripts/ alongside ingestion/:
#   docker build -f docker/excelreport/Dockerfile -t excelreport-ingestion .
#
# Run (mount your YCPT_BAOCAO workbooks + a workflow config):
#   docker run --rm \
#     --network ometa_network \
#     -v /path/to/report-requests:/data/report-requests:ro \
#     -v /path/to/workflow.yaml:/config/workflow.yaml:ro \
#     excelreport-ingestion
#
# --network ometa_network joins the network created by
# docker/development/docker-compose.yml, so the container can reach
# http://openmetadata-server:8585/api. Drop it (and point hostPort at
# host.docker.internal:8585 instead) if the server runs elsewhere.

# ---- builder: generate the Pydantic schema models + ANTLR FQN parser ----
FROM python:3.11-slim AS builder

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get -qq update \
  && apt-get -qq install -y --no-install-recommends default-jre-headless wget \
  && rm -rf /var/lib/apt/lists/*

# Pinned, checksum-verified ANTLR CLI - same version/jar as the root Makefile's
# install_antlr_cli target.
ARG ANTLR_VERSION=4.9.2
ARG ANTLR_JAR_SHA256=bb117b1476691dc2915a318efd36f8957c0ad93447fb1dac01107eb15fe137cd
RUN wget -q "https://repo1.maven.org/maven2/org/antlr/antlr4/${ANTLR_VERSION}/antlr4-${ANTLR_VERSION}-complete.jar" \
      -O /usr/local/lib/antlr4-complete.jar \
  && echo "${ANTLR_JAR_SHA256}  /usr/local/lib/antlr4-complete.jar" | sha256sum -c -

WORKDIR /openmetadata
COPY openmetadata-spec/src/main/resources/json/schema openmetadata-spec/src/main/resources/json/schema
COPY openmetadata-spec/src/main/antlr4 openmetadata-spec/src/main/antlr4
COPY scripts/datamodel_generation.py scripts/datamodel_generation.py
COPY ingestion ingestion

RUN pip install --no-cache-dir --upgrade pip "setuptools<81" \
  && pip install --no-cache-dir datamodel-code-generator==0.25.6 \
  && rm -rf ingestion/src/metadata/generated \
  && mkdir -p ingestion/src/metadata/generated \
  && python scripts/datamodel_generation.py \
  && java -jar /usr/local/lib/antlr4-complete.jar -Dlanguage=Python3 \
       -o ingestion/src/metadata/generated/antlr \
       $(pwd)/openmetadata-spec/src/main/antlr4/org/openmetadata/schema/*.g4

# ---- runtime: just the ingestion package + the generated code above ----
FROM python:3.11-slim

WORKDIR /openmetadata
COPY --from=builder /openmetadata/ingestion ingestion

RUN pip install --no-cache-dir --upgrade pip "setuptools<81" \
  && pip install --no-cache-dir -e "ingestion/" \
  && pip install --no-cache-dir openpyxl

ENTRYPOINT ["metadata"]
CMD ["ingest", "--config", "/config/workflow.yaml"]
