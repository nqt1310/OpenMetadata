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
Reusable scaffolding for log connectors.

See `README.md` in this package for the contract a concrete log connector
has to implement.
"""

from metadata.ingestion.source.messaging.logs.connection import (
    ConnectionCheck,
    get_connection_options,
    require_option,
    run_connection_checks,
)
from metadata.ingestion.source.messaging.logs.log_source import BaseLogSource
from metadata.ingestion.source.messaging.logs.models import LogStream

__all__ = [
    "BaseLogSource",
    "ConnectionCheck",
    "LogStream",
    "get_connection_options",
    "require_option",
    "run_connection_checks",
]
