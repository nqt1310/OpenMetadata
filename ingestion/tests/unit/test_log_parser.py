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
Test log body parsing and the JSON Schema inferred from a sample.
"""

import json

from metadata.parsers.logs import LogFormat, infer_log_schema, parse_log_body


def properties_of(bodies, name="stream", **kwargs):
    return infer_log_schema(bodies, schema_name=name, **kwargs).json_schema[
        "properties"
    ]


class TestJsonBodies:
    def test_object_body_is_split_into_fields(self):
        record = parse_log_body('{"level":"ERROR","code":500,"ok":false}')

        assert record.format is LogFormat.JSON
        assert record.fields == {"level": "ERROR", "code": 500, "ok": False}

    def test_json_is_recovered_from_behind_a_runtime_prefix(self):
        record = parse_log_body(
            '2026-08-06T10:11:12Z stdout F {"level":"INFO","msg":"up"}'
        )

        assert record.format is LogFormat.JSON
        assert record.fields == {"level": "INFO", "msg": "up"}

    def test_non_object_json_is_not_treated_as_structured(self):
        record = parse_log_body("[1, 2, 3]")

        assert record.format is LogFormat.RAW
        assert record.fields == {}


class TestLogfmtBodies:
    def test_pairs_are_split_and_scalars_coerced(self):
        record = parse_log_body(
            'ts=2026-08-06T10:11:12Z level=error msg="disk full" retries=3 ok=false'
        )

        assert record.format is LogFormat.LOGFMT
        assert record.fields == {
            "ts": "2026-08-06T10:11:12Z",
            "level": "error",
            "msg": "disk full",
            "retries": 3,
            "ok": False,
        }

    def test_prose_with_an_incidental_pair_is_not_logfmt(self):
        record = parse_log_body(
            "the request failed because timeout=30 was exceeded on the upstream"
        )

        assert record.format is LogFormat.RAW

    def test_a_single_pair_is_not_enough(self):
        record = parse_log_body("level=error")

        assert record.format is LogFormat.RAW


class TestPlaintextBodies:
    def test_timestamped_application_line(self):
        record = parse_log_body(
            "2026-08-06 10:11:12,123 [main] INFO com.foo.Bar - started"
        )

        assert record.format is LogFormat.PLAINTEXT
        assert record.pattern == "timestamped"
        assert record.fields == {
            "timestamp": "2026-08-06 10:11:12,123",
            "thread": "main",
            "level": "INFO",
            "logger": "com.foo.Bar",
            "message": "started",
        }

    def test_a_colon_inside_the_message_is_not_read_as_a_logger(self):
        record = parse_log_body(
            "2026-08-06T10:11:12Z ERROR something failed: user not found"
        )

        assert record.fields["message"] == "something failed: user not found"
        assert "logger" not in record.fields

    def test_bracketed_line(self):
        record = parse_log_body("[2026-08-06T10:11:12Z] [ERROR] disk full")

        assert record.pattern == "bracketed"
        assert record.fields == {
            "timestamp": "2026-08-06T10:11:12Z",
            "level": "ERROR",
            "message": "disk full",
        }

    def test_syslog_line(self):
        record = parse_log_body("Aug  6 10:11:12 host sshd[123]: accepted publickey")

        assert record.pattern == "syslog"
        assert record.fields["host"] == "host"
        assert record.fields["process"] == "sshd"
        assert record.fields["pid"] == 123

    def test_common_log_format_line(self):
        record = parse_log_body(
            '10.0.0.1 - - [06/Aug/2026:10:11:12 +0700] "GET / HTTP/1.1" 200 1234'
        )

        assert record.pattern == "clf"
        assert record.fields["client_ip"] == "10.0.0.1"
        assert record.fields["status"] == 200
        assert record.fields["bytes_sent"] == 1234

    def test_free_form_text_stays_raw(self):
        record = parse_log_body("Starting the thing now, hold on")

        assert record.format is LogFormat.RAW
        assert record.fields == {}


class TestSchemaInference:
    def test_fields_are_unioned_across_the_sample(self):
        properties = properties_of(
            [
                '{"level":"ERROR","msg":"boom"}',
                '{"level":"INFO","latency_ms":12}',
            ]
        )

        assert set(properties) == {"level", "msg", "latency_ms"}
        assert properties["latency_ms"] == {"type": "integer"}

    def test_integer_and_float_widen_to_number(self):
        properties = properties_of(['{"latency":12}', '{"latency":1.5}'])

        assert properties["latency"] == {"type": "number"}

    def test_null_never_wins_over_a_real_type(self):
        properties = properties_of(['{"user":null}', '{"user":"alice"}'])

        assert properties["user"] == {"type": "string"}

    def test_conflicting_types_collapse_to_unknown(self):
        properties = properties_of(['{"id":1}', '{"id":{"raw":"x"}}'])

        assert properties["id"] == {"type": "unknown"}

    def test_nested_objects_are_merged_key_by_key(self):
        properties = properties_of(
            [
                '{"ctx":{"user":1,"ok":true}}',
                '{"ctx":{"user":2,"role":"admin"}}',
            ]
        )

        assert properties["ctx"]["type"] == "object"
        assert set(properties["ctx"]["properties"]) == {"user", "ok", "role"}
        assert properties["ctx"]["properties"]["role"] == {"type": "string"}

    def test_array_items_are_typed(self):
        properties = properties_of(['{"tags":["a","b"]}'])

        assert properties["tags"] == {"type": "array", "items": {"type": "string"}}

    def test_iso_strings_are_marked_as_timestamps(self):
        properties = properties_of(['{"ts":"2026-08-06T10:11:12Z","msg":"hi"}'])

        assert properties["ts"]["format"] == "date-time"
        assert "format" not in properties["msg"]

    def test_the_dominant_format_decides_the_schema(self):
        inferred = infer_log_schema(
            [
                '{"level":"ERROR"}',
                '{"level":"INFO"}',
                "Aug  6 10:11:12 host sshd[123]: hello",
            ]
        )

        assert inferred.format is LogFormat.JSON
        assert inferred.sampled_records == 3
        assert inferred.parsed_records == 2
        assert set(inferred.json_schema["properties"]) == {"level"}

    def test_plaintext_patterns_are_reported(self):
        inferred = infer_log_schema(
            [
                "Aug  6 10:11:12 host sshd[123]: one",
                "Aug  6 10:11:13 host sshd[124]: two",
            ]
        )

        assert inferred.format is LogFormat.PLAINTEXT
        assert inferred.patterns == ["syslog"]

    def test_unrecognised_sample_falls_back_to_a_message_field(self):
        inferred = infer_log_schema(["just some text", "more text"])

        assert inferred.format is LogFormat.RAW
        assert inferred.parsed_records == 0
        assert inferred.json_schema["properties"] == {"message": {"type": "string"}}

    def test_empty_sample_still_produces_a_usable_schema(self):
        inferred = infer_log_schema([])

        assert inferred.format is LogFormat.RAW
        assert inferred.sampled_records == 0
        assert inferred.json_schema["properties"] == {"message": {"type": "string"}}

    def test_schema_text_is_valid_json_carrying_the_stream_name(self):
        inferred = infer_log_schema(['{"level":"ERROR"}'], schema_name="app.log")

        parsed = json.loads(inferred.schema_text)
        assert parsed["title"] == "app.log"
        assert parsed["type"] == "object"


class TestJsonStringPayloads:
    """A payload carried as an escaped JSON string is the whole point."""

    ENVELOPE = (
        '{"application":"ms01-api-extract-address-data",'
        '"levelname":"INFO",'
        '"body":"{\\"result\\":1,\\"ward_code\\":\\"00385\\",'
        '\\"street\\":\\"PHUC XUAN\\"}"}'
    )

    def test_body_string_is_expanded_into_its_own_fields(self):
        properties = properties_of([self.ENVELOPE])

        body = properties["body"]
        assert body["type"] == "object"
        assert set(body["properties"]) == {"result", "ward_code", "street"}
        assert body["properties"]["result"] == {"type": "integer"}

    def test_expanded_body_records_that_the_source_type_is_text(self):
        properties = properties_of([self.ENVELOPE])

        assert properties["body"]["contentMediaType"] == "application/json"

    def test_expansion_can_be_turned_off(self):
        properties = properties_of([self.ENVELOPE], expand_json_strings=False)

        assert properties["body"] == {"type": "string"}

    def test_body_fields_are_unioned_across_records(self):
        properties = properties_of(
            [
                '{"body":"{\\"a\\":1}"}',
                '{"body":"{\\"b\\":\\"x\\"}"}',
            ]
        )

        assert set(properties["body"]["properties"]) == {"a", "b"}

    def test_a_plain_string_field_is_left_alone(self):
        properties = properties_of(['{"message":"not json at all"}'])

        assert properties["message"] == {"type": "string"}
