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
Reusable parsing of raw log bodies into a schema OpenMetadata can store.

Log sources rarely publish a schema, so the shape of a log stream has to be
recovered from the payload itself. This package turns a handful of sampled
log lines into a JSON Schema, which the existing
`metadata.parsers.json_schema_parser` then converts into entity fields.

Nothing here imports generated models or talks to a broker, so it can be
reused by any log connector and unit tested on its own.
"""

from metadata.parsers.logs.formats import (
    detect_log_format,
    parse_json_object,
    parse_log_bodies,
    parse_log_body,
)
from metadata.parsers.logs.inference import infer_log_schema
from metadata.parsers.logs.models import InferredLogSchema, LogFormat, ParsedLogRecord

__all__ = [
    "InferredLogSchema",
    "LogFormat",
    "ParsedLogRecord",
    "detect_log_format",
    "infer_log_schema",
    "parse_json_object",
    "parse_log_bodies",
    "parse_log_body",
]
