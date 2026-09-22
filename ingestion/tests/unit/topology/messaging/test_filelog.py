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
Test the file log connector and, through it, the reusable `BaseLogSource`.

Only the OpenMetadata server is mocked: the folder scanning, sampling,
parsing and schema inference all run for real against files on disk.
"""

import uuid
from unittest.mock import MagicMock

import pytest

from metadata.generated.schema.entity.data.topic import Topic
from metadata.generated.schema.entity.services.connections.messaging.customMessagingConnection import (
    CustomMessagingConnection,
)
from metadata.generated.schema.entity.services.connections.testConnectionResult import (
    StatusType,
)
from metadata.generated.schema.type.entityReference import EntityReference
from metadata.generated.schema.type.schema import SchemaType
from metadata.ingestion.connections.test_connections import SourceConnectionException

# Imported as a module: `test_connection` is a connector entrypoint, and
# importing it by name would have pytest collect it as a test case.
from metadata.ingestion.source.messaging.filelog import metadata as filelog
from metadata.ingestion.source.messaging.filelog.metadata import FileLogSource
from metadata.ingestion.source.messaging.logs.models import LogStream

SERVICE_NAME = "test_file_logs"

JSON_LINES = [
    '{"ts":"2026-08-06T10:11:12Z","level":"ERROR","msg":"boom","ctx":{"user":1}}',
    '{"ts":"2026-08-06T10:11:13Z","level":"INFO","msg":"fine",'
    '"ctx":{"user":2,"role":"admin"}}',
]


def build_connection(directory, **options):
    return CustomMessagingConnection.model_validate(
        {
            "type": "CustomMessaging",
            "sourcePythonClass": (
                "metadata.ingestion.source.messaging.filelog.metadata.FileLogSource"
            ),
            "connectionOptions": {"directoryPath": str(directory), **options},
        }
    )


def build_source(directory, **options):
    config = {
        "type": "custommessaging",
        "serviceName": SERVICE_NAME,
        "serviceConnection": {
            "config": build_connection(directory, **options).model_dump()
        },
        "sourceConfig": {
            "config": {"type": "MessagingMetadata", "generateSampleData": True}
        },
    }
    source = FileLogSource.create(config, MagicMock())
    source.context.get().__dict__["messaging_service"] = SERVICE_NAME
    return source


def write_log(directory, name, lines):
    path = directory / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def log_dir(tmp_path):
    write_log(tmp_path, "app.log", JSON_LINES)
    return tmp_path


class TestConnectionHandling:
    def test_missing_directory_path_is_rejected(self):
        connection = build_connection("")
        connection.connectionOptions.root.clear()

        with pytest.raises(SourceConnectionException, match="directoryPath"):
            filelog.get_connection(connection)

    def test_a_path_that_is_not_a_directory_is_rejected(self, tmp_path):
        target = tmp_path / "app.log"
        target.write_text("hello\n", encoding="utf-8")

        with pytest.raises(SourceConnectionException, match="not a directory"):
            filelog.get_connection(build_connection(target))

    def test_file_pattern_defaults_to_log_files(self, tmp_path):
        write_log(tmp_path, "app.log", ["hello"])
        write_log(tmp_path, "notes.txt", ["hello"])

        client = filelog.get_connection(build_connection(tmp_path))

        assert [path.name for path in client.list_files()] == ["app.log"]

    def test_sub_folders_are_scanned_only_when_recursive(self, tmp_path):
        nested = tmp_path / "nested"
        nested.mkdir()
        write_log(nested, "deep.log", ["hello"])

        assert filelog.get_connection(build_connection(tmp_path)).list_files() == []

        recursive = filelog.get_connection(build_connection(tmp_path, recursive="true"))
        assert [path.name for path in recursive.list_files()] == ["deep.log"]

    def test_test_connection_fails_on_an_empty_folder(self, tmp_path):
        client = filelog.get_connection(build_connection(tmp_path))

        result = filelog.test_connection(
            MagicMock(), client, build_connection(tmp_path)
        )

        assert result.status == StatusType.Failed
        assert [step.name for step in result.steps if not step.passed] == [
            "ListLogFiles"
        ]

    def test_test_connection_passes_once_a_log_file_exists(self, log_dir):
        client = filelog.get_connection(build_connection(log_dir))

        result = filelog.test_connection(MagicMock(), client, build_connection(log_dir))

        assert result.status == StatusType.Successful
        assert all(step.passed for step in result.steps)


class TestStreamDiscovery:
    def test_one_stream_per_file_carrying_its_path(self, log_dir):
        streams = list(build_source(log_dir).get_log_streams())

        assert len(streams) == 1
        assert streams[0].name == "app.log"
        assert streams[0].display_name == "app"
        assert streams[0].attributes["path"] == str(log_dir / "app.log")
        assert streams[0].attributes["sizeBytes"] > 0

    def test_sample_is_capped_at_the_requested_limit(self, tmp_path):
        write_log(tmp_path, "app.log", [f'{{"n":{index}}}' for index in range(20)])
        source = build_source(tmp_path)
        stream = next(iter(source.get_log_streams()))

        assert len(list(source.fetch_log_sample(stream, 3))) == 3

    def test_blank_lines_are_skipped(self, tmp_path):
        write_log(tmp_path, "app.log", ['{"a":1}', "", "   ", '{"a":2}'])
        source = build_source(tmp_path)
        stream = next(iter(source.get_log_streams()))

        assert len(list(source.fetch_log_sample(stream, 10))) == 2


class TestSampleSize:
    def test_defaults_when_unset(self, log_dir):
        assert build_source(log_dir).sample_size == 10

    def test_read_from_connection_options(self, log_dir):
        assert build_source(log_dir, sampleSize="3").sample_size == 3

    def test_clamped_to_the_supported_range(self, log_dir):
        assert build_source(log_dir, sampleSize="5000").sample_size == 100
        assert build_source(log_dir, sampleSize="0").sample_size == 1

    def test_a_non_numeric_value_falls_back_to_the_default(self, log_dir):
        assert build_source(log_dir, sampleSize="lots").sample_size == 10


class TestTopicCreation:
    def test_schema_is_inferred_from_the_sampled_bodies(self, log_dir):
        topic = next(
            iter(build_source(log_dir).yield_topic(_first_stream(log_dir)))
        ).right

        assert topic.name.root == "app.log"
        assert topic.service.root == SERVICE_NAME
        assert topic.messageSchema.schemaType == SchemaType.JSON

        root_field = topic.messageSchema.schemaFields[0]
        assert root_field.name.root == "app.log"
        assert {child.name.root for child in root_field.children} == {
            "ts",
            "level",
            "msg",
            "ctx",
        }

    def test_nested_objects_survive_into_topic_fields(self, log_dir):
        topic = next(
            iter(build_source(log_dir).yield_topic(_first_stream(log_dir)))
        ).right

        context_field = next(
            child
            for child in topic.messageSchema.schemaFields[0].children
            if child.name.root == "ctx"
        )
        assert {child.name.root for child in context_field.children} == {
            "user",
            "role",
        }

    def test_json_string_payload_is_expanded_into_child_fields(self, tmp_path):
        write_log(
            tmp_path,
            "app.log",
            [
                '{"application":"svc","body":"{\\"result\\":1,\\"ward_code\\":\\"00385\\"}"}'
            ],
        )
        topic = next(
            iter(build_source(tmp_path).yield_topic(_first_stream(tmp_path)))
        ).right

        body_field = next(
            child
            for child in topic.messageSchema.schemaFields[0].children
            if child.name.root == "body"
        )
        assert {child.name.root for child in body_field.children} == {
            "result",
            "ward_code",
        }

    def test_topic_config_reports_what_the_sampling_found(self, log_dir):
        topic = next(
            iter(build_source(log_dir).yield_topic(_first_stream(log_dir)))
        ).right

        config = topic.topicConfig
        assert config["logFormat"] == "json"
        assert config["sampledRecords"] == 2
        assert config["parsedRecords"] == 2
        assert config["path"] == str(log_dir / "app.log")

    def test_unstructured_logs_are_reported_as_raw(self, tmp_path):
        write_log(tmp_path, "app.log", ["starting up", "still going"])
        topic = next(
            iter(build_source(tmp_path).yield_topic(_first_stream(tmp_path)))
        ).right

        assert topic.topicConfig["logFormat"] == "raw"
        assert topic.topicConfig["parsedRecords"] == 0
        assert [
            child.name.root for child in topic.messageSchema.schemaFields[0].children
        ] == ["message"]

    def test_a_failing_stream_is_reported_instead_of_raised(self, log_dir):
        source = build_source(log_dir)
        stream = _first_stream(log_dir)
        stream.handle = log_dir / "gone.log"

        result = next(iter(source.yield_topic(stream)))

        assert result.right is None
        assert "gone.log" in result.left.stackTrace


class TestSampleData:
    def test_raw_bodies_are_stored_verbatim(self, log_dir):
        source = build_source(log_dir)
        source.context.get().__dict__["topic"] = "app.log"
        source.metadata.get_by_name.return_value = _topic_entity()

        result = next(iter(source.yield_topic_sample_data(_first_stream(log_dir))))

        assert result.right.sample_data.messages == JSON_LINES

    def test_nothing_is_stored_when_sample_data_is_off(self, log_dir):
        source = build_source(log_dir)
        source.generate_sample_data = False

        assert list(source.yield_topic_sample_data(_first_stream(log_dir))) == []


def _first_stream(directory):
    path = directory / "app.log"
    return LogStream(
        name=path.name,
        display_name=path.stem,
        attributes={"path": str(path)},
        handle=path,
    )


def _topic_entity():
    return Topic(
        id=uuid.uuid4(),
        name="app.log",
        service=EntityReference(id=uuid.uuid4(), type="messagingService"),
        partitions=1,
    )
