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
Base class turning any log source into OpenMetadata Topics.

Log backends do not publish schemas, so this source recovers one: it pulls a
small sample of bodies off each stream, parses them
(`metadata.parsers.logs`), and stores the inferred JSON Schema on the topic's
`messageSchema`. Subclasses only answer two questions -- which streams exist,
and how to read a few lines off one.
"""

import traceback
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, List, Optional

from metadata.generated.schema.api.data.createTopic import CreateTopicRequest
from metadata.generated.schema.entity.data.topic import Topic as TopicEntity
from metadata.generated.schema.entity.data.topic import TopicSampleData
from metadata.generated.schema.entity.services.ingestionPipelines.status import (
    StackTraceError,
)
from metadata.generated.schema.metadataIngestion.workflow import (
    Source as WorkflowSource,
)
from metadata.generated.schema.type.basic import (
    EntityName,
    FullyQualifiedEntityName,
    Markdown,
)
from metadata.generated.schema.type.schema import SchemaType
from metadata.generated.schema.type.schema import Topic as MessageSchema
from metadata.ingestion.api.models import Either
from metadata.ingestion.models.ometa_topic_data import OMetaTopicSampleData
from metadata.ingestion.ometa.ometa_api import OpenMetadata
from metadata.ingestion.source.messaging.logs.connection import get_connection_options
from metadata.ingestion.source.messaging.logs.models import LogStream
from metadata.ingestion.source.messaging.messaging_service import MessagingServiceSource
from metadata.parsers.logs import InferredLogSchema, LogFormat, infer_log_schema
from metadata.parsers.schema_parsers import schema_parser_config_registry
from metadata.utils import fqn
from metadata.utils.logger import ingestion_logger

logger = ingestion_logger()

SAMPLE_SIZE_KEY = "sampleSize"
DEFAULT_SAMPLE_SIZE = 10
MIN_SAMPLE_SIZE = 1
# Schema inference reads every sampled body; a large sample buys little
# accuracy and makes listing a wide catalogue slow.
MAX_SAMPLE_SIZE = 100

LOG_FORMAT_ATTRIBUTE = "logFormat"
LOG_PATTERNS_ATTRIBUTE = "logPatterns"
SAMPLED_RECORDS_ATTRIBUTE = "sampledRecords"
PARSED_RECORDS_ATTRIBUTE = "parsedRecords"


class BaseLogSource(MessagingServiceSource, ABC):
    """
    Ingest log streams as Topics carrying an inferred message schema.

    Concrete connectors implement `get_log_streams` and `fetch_log_sample`
    and expose module-level `get_connection` / `test_connection`, which is
    how custom connectors are resolved from `sourcePythonClass`.
    """

    def __init__(self, config: WorkflowSource, metadata: OpenMetadata):
        super().__init__(config, metadata)
        self.sample_size = self._read_sample_size()
        self.generate_sample_data = bool(
            getattr(self.source_config, "generateSampleData", False)
        )
        if (
            self.generate_sample_data
            and self._is_sample_data_storing_globally_disabled()
        ):
            self.generate_sample_data = False
        # Single-entry memo, not a cache: the topology samples a stream in
        # `yield_topic` and reuses it in `yield_topic_sample_data` before
        # moving on, so only the current stream is ever held.
        self._sampled_stream: Optional[str] = None
        self._sampled_bodies: List[str] = []

    @property
    def connection_options(self) -> Dict[str, str]:
        """Connector-specific options declared on the service connection."""
        return get_connection_options(self.service_connection)

    @abstractmethod
    def get_log_streams(self) -> Iterable[LogStream]:
        """
        List the log streams exposed by this source.

        One stream becomes one Topic. Filtering against
        `topicFilterPattern` is applied by the caller.
        """

    @abstractmethod
    def fetch_log_sample(self, stream: LogStream, limit: int) -> Iterable[str]:
        """
        Read up to `limit` raw log bodies off `stream`.

        Bodies are the log lines as the source stores them; decoding and
        de-framing belong here, parsing does not. Returning fewer than
        `limit` is fine, and an empty result yields a topic with a
        placeholder schema rather than an error.
        """

    def get_topic_list(self) -> List[LogStream]:
        return list(self.get_log_streams())

    def get_topic_name(self, topic_details: LogStream) -> str:
        return topic_details.name

    def yield_topic(
        self, topic_details: LogStream
    ) -> Iterable[Either[CreateTopicRequest]]:
        try:
            inferred = self._infer_stream_schema(topic_details)
            topic = CreateTopicRequest(
                name=EntityName(topic_details.name),
                displayName=topic_details.display_name,
                description=(
                    Markdown(topic_details.description)
                    if topic_details.description
                    else None
                ),
                service=FullyQualifiedEntityName(self.context.get().messaging_service),
                partitions=topic_details.partitions,
                messageSchema=self._build_message_schema(topic_details, inferred),
            )
            # Assigned after construction, as the broker sources do: the
            # generated `TopicConfig` declares no fields, so a dict passed
            # through the constructor is validated away to nothing.
            topic.topicConfig = self._build_topic_config(topic_details, inferred)
            yield Either(right=topic)
            self.register_record(topic_request=topic)
        except Exception as exc:
            yield Either(
                left=StackTraceError(
                    name=topic_details.name,
                    error=(
                        f"Unexpected exception to yield log stream "
                        f"[{topic_details.name}]: {exc}"
                    ),
                    stackTrace=traceback.format_exc(),
                )
            )

    def yield_topic_sample_data(
        self, topic_details: LogStream
    ) -> Iterable[Either[TopicSampleData]]:
        """Store the sampled bodies verbatim, so the raw log stays readable."""
        if self.generate_sample_data:
            topic_fqn = fqn.build(
                metadata=self.metadata,
                entity_type=TopicEntity,
                service_name=self.context.get().messaging_service,
                topic_name=self.context.get().topic or topic_details.name,
            )
            topic_entity = (
                self.metadata.get_by_name(entity=TopicEntity, fqn=topic_fqn)
                if topic_fqn
                else None
            )
            if topic_entity:
                try:
                    bodies = self._sample_bodies(topic_details)
                except Exception as exc:
                    yield Either(
                        left=StackTraceError(
                            name=topic_details.name,
                            error=(
                                f"Failed to fetch sample data from log stream "
                                f"{topic_details.name}: {exc}"
                            ),
                            stackTrace=traceback.format_exc(),
                        )
                    )
                else:
                    yield Either(
                        right=OMetaTopicSampleData(
                            topic=topic_entity,
                            sample_data=TopicSampleData(messages=bodies),
                        )
                    )

    def _read_sample_size(self) -> int:
        raw_value = self.connection_options.get(SAMPLE_SIZE_KEY)
        sample_size = DEFAULT_SAMPLE_SIZE
        if raw_value is not None:
            try:
                sample_size = int(raw_value)
            except (TypeError, ValueError):
                logger.warning(
                    f"Ignoring non-numeric {SAMPLE_SIZE_KEY} '{raw_value}', "
                    f"using {DEFAULT_SAMPLE_SIZE}"
                )
        return max(MIN_SAMPLE_SIZE, min(sample_size, MAX_SAMPLE_SIZE))

    def _sample_bodies(self, stream: LogStream) -> List[str]:
        if self._sampled_stream != stream.name:
            bodies: List[str] = []
            for body in self.fetch_log_sample(stream, self.sample_size):
                if body and body.strip():
                    bodies.append(body.strip())
                if len(bodies) >= self.sample_size:
                    break
            self._sampled_stream = stream.name
            self._sampled_bodies = bodies
        return self._sampled_bodies

    def _infer_stream_schema(self, stream: LogStream) -> InferredLogSchema:
        return infer_log_schema(self._sample_bodies(stream), schema_name=stream.name)

    @staticmethod
    def _build_message_schema(
        stream: LogStream, inferred: InferredLogSchema
    ) -> MessageSchema:
        """
        Convert the inferred JSON Schema into topic fields.

        Reuses the registered JSON Schema parser so nested records and
        arrays are shaped exactly like those of a schema-registry topic.
        """
        parse_json_schema = schema_parser_config_registry.registry.get(
            SchemaType.JSON.value.lower()
        )
        schema_fields = parse_json_schema(stream.name, inferred.schema_text)
        return MessageSchema(
            schemaText=inferred.schema_text,
            schemaType=SchemaType.JSON,
            schemaFields=schema_fields or [],
        )

    @staticmethod
    def _build_topic_config(
        stream: LogStream, inferred: InferredLogSchema
    ) -> Dict[str, Any]:
        """
        Publish what the sampling actually found alongside the source's own
        attributes, so a low `parsedRecords` is visible rather than implied.
        """
        config: Dict[str, Any] = dict(stream.attributes)
        config[LOG_FORMAT_ATTRIBUTE] = inferred.format.value
        config[SAMPLED_RECORDS_ATTRIBUTE] = inferred.sampled_records
        config[PARSED_RECORDS_ATTRIBUTE] = inferred.parsed_records
        if inferred.patterns:
            config[LOG_PATTERNS_ATTRIBUTE] = ",".join(inferred.patterns)
        if inferred.format is LogFormat.RAW and inferred.sampled_records:
            logger.warning(
                f"No structure recognised in {inferred.sampled_records} sampled "
                f"lines of '{stream.name}'; storing it as a single message field"
            )
        return config
