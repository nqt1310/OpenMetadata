#  Copyright 2025 Collate
#  Licensed under the Collate Community License, Version 1.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  https://github.com/open-metadata/OpenMetadata/blob/main/ingestion/LICENSE
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""
Catalogue log files from a local or mounted folder.

Reference implementation of `BaseLogSource`, and the connector to copy when
adding a new log backend: everything specific to "files on a disk" lives in
`get_log_streams` / `fetch_log_sample`, while sampling, parsing, schema
inference and entity creation come from the base class.

Wired as a `CustomMessagingConnection` (`sourcePythonClass` pointing at
`FileLogSource`), so no JSON Schema / Java / UI change is needed to use it.
`connectionOptions`:
  * directoryPath (required): folder to scan for log files
  * filePattern (optional, default "*.log")
  * recursive (optional, default "false"): also scan sub-folders
  * sampleSize (optional, default 10): lines read per file to infer a schema
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from metadata.generated.schema.entity.automations.workflow import (
    Workflow as AutomationWorkflow,
)
from metadata.generated.schema.entity.services.connections.messaging.customMessagingConnection import (
    CustomMessagingConnection,
)
from metadata.generated.schema.entity.services.connections.testConnectionResult import (
    TestConnectionResult,
)
from metadata.generated.schema.metadataIngestion.workflow import (
    Source as WorkflowSource,
)
from metadata.ingestion.api.steps import InvalidSourceException
from metadata.ingestion.connections.test_connections import SourceConnectionException
from metadata.ingestion.ometa.ometa_api import OpenMetadata
from metadata.ingestion.source.messaging.logs.connection import (
    ConnectionCheck,
    get_connection_options,
    require_option,
    run_connection_checks,
)
from metadata.ingestion.source.messaging.logs.log_source import BaseLogSource
from metadata.ingestion.source.messaging.logs.models import LogStream
from metadata.utils.constants import THREE_MIN
from metadata.utils.logger import ingestion_logger

logger = ingestion_logger()

DIRECTORY_PATH_KEY = "directoryPath"
FILE_PATTERN_KEY = "filePattern"
RECURSIVE_KEY = "recursive"

DEFAULT_FILE_PATTERN = "*.log"

PATH_ATTRIBUTE = "path"
SIZE_ATTRIBUTE = "sizeBytes"
MODIFIED_AT_ATTRIBUTE = "modifiedAt"


@dataclass(frozen=True)
class FileLogClient:
    """Resolved folder to scan, and how to scan it."""

    directory: Path
    file_pattern: str
    recursive: bool

    def list_files(self) -> List[Path]:
        matcher = self.directory.rglob if self.recursive else self.directory.glob
        return sorted(path for path in matcher(self.file_pattern) if path.is_file())


def get_connection(connection: CustomMessagingConnection) -> FileLogClient:
    options = get_connection_options(connection)
    directory = require_option(options, DIRECTORY_PATH_KEY, "locate the log files")
    directory_path = Path(directory)
    if not directory_path.is_dir():
        raise SourceConnectionException(
            f"'{directory}' is not a directory OpenMetadata can read"
        )
    return FileLogClient(
        directory=directory_path,
        file_pattern=options.get(FILE_PATTERN_KEY) or DEFAULT_FILE_PATTERN,
        recursive=str(options.get(RECURSIVE_KEY, "")).lower() == "true",
    )


def _require_log_files(client: FileLogClient) -> None:
    if not client.list_files():
        raise SourceConnectionException(
            f"No files matching '{client.file_pattern}' found in '{client.directory}'"
        )


def _read_sample_file(client: FileLogClient) -> None:
    files = client.list_files()
    if files:
        with files[0].open("r", encoding="utf-8", errors="replace") as handle:
            handle.readline()


def test_connection(  # pylint: disable=unused-argument
    metadata: OpenMetadata,
    client: FileLogClient,
    service_connection: CustomMessagingConnection,
    automation_workflow: Optional[AutomationWorkflow] = None,
    timeout_seconds: Optional[int] = THREE_MIN,
) -> TestConnectionResult:
    """
    Signature is fixed by `import_connection_fn`, so the unused arguments
    stay: the checks below need only the client this module built.
    """
    checks = (
        ConnectionCheck(name="ListLogFiles", mandatory=True, run=_require_log_files),
        ConnectionCheck(name="ReadSampleLines", mandatory=False, run=_read_sample_file),
    )
    return run_connection_checks(metadata, client, checks, automation_workflow)


class FileLogSource(BaseLogSource):
    """One Topic per log file, with a schema inferred from its first lines."""

    @classmethod
    def create(
        cls,
        config_dict: dict,
        metadata: OpenMetadata,
        pipeline_name: Optional[str] = None,
    ) -> "FileLogSource":
        config = WorkflowSource.model_validate(config_dict)
        connection = config.serviceConnection.root.config
        if not isinstance(connection, CustomMessagingConnection):
            raise InvalidSourceException(
                f"Expected CustomMessagingConnection, but got {connection}"
            )
        return cls(config, metadata)

    def get_log_streams(self) -> Iterable[LogStream]:
        for path in self.connection.list_files():
            yield LogStream(
                name=path.name,
                display_name=path.stem,
                attributes=self._file_attributes(path),
                handle=path,
            )

    def fetch_log_sample(self, stream: LogStream, limit: int) -> Iterator[str]:
        """
        Read the first `limit` non-empty lines.

        Reading the head rather than the tail keeps the cost independent of
        file size; log files are append-only and keep their format, so the
        first lines describe the stream as well as the last ones.
        """
        path: Path = stream.handle
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            taken = 0
            for line in handle:
                if line.strip():
                    yield line
                    taken += 1
                if taken >= limit:
                    break

    def _file_attributes(self, path: Path) -> Dict[str, Any]:
        attributes: Dict[str, Any] = {PATH_ATTRIBUTE: str(path)}
        try:
            stats = path.stat()
            attributes[SIZE_ATTRIBUTE] = stats.st_size
            attributes[MODIFIED_AT_ATTRIBUTE] = datetime.fromtimestamp(
                stats.st_mtime, tz=timezone.utc
            ).isoformat()
        except OSError as exc:
            logger.warning(f"Could not stat log file '{path}': {exc}")
        return attributes
