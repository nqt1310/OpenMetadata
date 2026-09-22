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
Models shared by the log body parsers and the schema inference.
"""

import json
from enum import Enum
from typing import Any, Dict, List

from pydantic import BaseModel, Field


class LogFormat(str, Enum):
    """
    Encoding of a log body, in the order the detector tries them.

    `RAW` is the terminal fallback: the line carries no structure we can
    recover, so it is kept as an opaque message.
    """

    JSON = "json"
    LOGFMT = "logfmt"
    PLAINTEXT = "plaintext"
    RAW = "raw"


class ParsedLogRecord(BaseModel):
    """
    A single log line after its body has been split into fields.

    `pattern` names the concrete plaintext pattern that matched (for example
    `clf` or `syslog`) and is empty for the other formats.
    """

    format: LogFormat
    fields: Dict[str, Any] = Field(default_factory=dict)
    body: str
    pattern: str = ""


class InferredLogSchema(BaseModel):
    """
    JSON Schema recovered from a sample of log bodies.

    `parsed_records` counts the sampled bodies that matched the winning
    format; a value well below `sampled_records` means the stream is mixed
    and the schema only describes part of it.
    """

    format: LogFormat
    json_schema: Dict[str, Any]
    sampled_records: int
    parsed_records: int
    patterns: List[str] = Field(default_factory=list)

    @property
    def schema_text(self) -> str:
        """Serialized schema, stored verbatim on the entity's `schemaText`."""
        return json.dumps(
            self.json_schema, indent=2, ensure_ascii=False, sort_keys=True
        )
