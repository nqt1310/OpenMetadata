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
Source-agnostic description of a log stream.

A "stream" is whatever unit of logs a connector lists and samples: a file,
a Kubernetes container, a CloudWatch log group, an Elasticsearch index
pattern. It maps onto one OpenMetadata Topic.
"""

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class LogStream(BaseModel):
    """
    One addressable unit of logs, before any of its bodies are read.

    `handle` carries whatever the connector needs to fetch the sample later
    (a path, a client, an ARN). The base source never inspects it; it only
    hands it back to `fetch_log_sample`.
    """

    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    partitions: int = 1
    attributes: Dict[str, Any] = Field(default_factory=dict)
    handle: Any = None
