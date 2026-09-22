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
Turn a sample of parsed log records into a JSON Schema.

The output is deliberately a JSON Schema and not a list of entity fields:
`metadata.parsers.json_schema_parser` already converts one into the other,
so log connectors inherit nested-record and array handling for free and the
schema stays readable on the entity as `schemaText`.
"""

from collections import Counter
from typing import Any, Dict, Iterable, Sequence

from metadata.parsers.logs.formats import (
    is_timestamp_like,
    parse_json_object,
    parse_log_bodies,
)
from metadata.parsers.logs.models import InferredLogSchema, LogFormat, ParsedLogRecord

JSON_SCHEMA_DRAFT = "http://json-schema.org/draft-07/schema#"

DEFAULT_SCHEMA_NAME = "log_record"

# Not a JSON Schema type, but the value `JsonSchemaDataTypes` maps to the
# UNKNOWN field type. Used when a key holds conflicting types across samples.
UNKNOWN_TYPE = "unknown"

RAW_MESSAGE_FIELD = "message"

# Marks a field whose value is a JSON document carried as a string, so the
# schema stays honest that the source type is text, not a native object.
JSON_STRING_MEDIA_TYPE = "application/json"

_NUMERIC_WIDENING = {"integer", "number"}


def _node_for_value(value: Any, expand_json_strings: bool) -> Dict[str, Any]:
    """JSON Schema node describing a single observed value."""
    if value is None:
        node = {"type": "null"}
    elif isinstance(value, bool):
        # bool before int: bool is an int subclass in Python.
        node = {"type": "boolean"}
    elif isinstance(value, int):
        node = {"type": "integer"}
    elif isinstance(value, float):
        node = {"type": "number"}
    elif isinstance(value, str):
        node = _node_for_string(value, expand_json_strings)
    elif isinstance(value, dict):
        node = _object_node(value, expand_json_strings)
    elif isinstance(value, list):
        node = {
            "type": "array",
            "items": _merge_many(
                _node_for_value(item, expand_json_strings) for item in value
            ),
        }
    else:
        node = {"type": UNKNOWN_TYPE}
    return node


def _node_for_string(value: str, expand_json_strings: bool) -> Dict[str, Any]:
    """
    Describe a string, looking inside it when it carries a JSON document.

    Log envelopes routinely ship the interesting payload as an escaped JSON
    string in a `body` / `payload` field, which the transport records as
    plain text. Expanding it is the only way that structure reaches the
    catalogue.
    """
    nested = parse_json_object(value) if expand_json_strings else None
    if nested is not None:
        node = _object_node(nested, expand_json_strings)
        node["contentMediaType"] = JSON_STRING_MEDIA_TYPE
    else:
        node = {"type": "string"}
        if is_timestamp_like(value):
            node["format"] = "date-time"
    return node


def _object_node(value: Dict[str, Any], expand_json_strings: bool) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            str(key): _node_for_value(item, expand_json_strings)
            for key, item in value.items()
        },
    }


def _merge_object_nodes(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    properties = dict(left.get("properties", {}))
    for key, node in right.get("properties", {}).items():
        properties[key] = (
            _merge_nodes(properties[key], node) if key in properties else node
        )
    merged = {"type": "object", "properties": properties}
    if left.get("contentMediaType") == right.get("contentMediaType") and left.get(
        "contentMediaType"
    ):
        merged["contentMediaType"] = left["contentMediaType"]
    return merged


def _merge_nodes(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    """
    Combine two observations of the same key.

    `null` never wins: a key that is null in one record and typed in another
    keeps the type. Genuinely conflicting types collapse to `unknown` rather
    than to a union, which would not survive the JSON Schema field parser.
    """
    left_type = left.get("type")
    right_type = right.get("type")
    if not left or left_type == "null":
        merged = right
    elif not right or right_type == "null":
        merged = left
    elif left_type != right_type:
        merged = (
            {"type": "number"}
            if {left_type, right_type} == _NUMERIC_WIDENING
            else {"type": UNKNOWN_TYPE}
        )
    elif left_type == "object":
        merged = _merge_object_nodes(left, right)
    elif left_type == "array":
        merged = {
            "type": "array",
            "items": _merge_nodes(left.get("items", {}), right.get("items", {})),
        }
    else:
        merged = (
            left if left.get("format") == right.get("format") else {"type": left_type}
        )
    return merged


def _merge_many(nodes: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for node in nodes:
        merged = _merge_nodes(merged, node)
    return merged


def _dominant_format(records: Sequence[ParsedLogRecord]) -> LogFormat:
    """
    Format shared by most parsed records.

    Ties break toward the earlier `LogFormat` member, so a stream that is
    half JSON and half plaintext is described as JSON.
    """
    counts = Counter(
        record.format for record in records if record.format is not LogFormat.RAW
    )
    dominant = LogFormat.RAW
    if counts:
        highest = max(counts.values())
        dominant = next(fmt for fmt in LogFormat if counts.get(fmt) == highest)
    return dominant


def _properties_for(
    records: Sequence[ParsedLogRecord], expand_json_strings: bool
) -> Dict[str, Any]:
    merged = _merge_many(
        _object_node(record.fields, expand_json_strings) for record in records
    )
    return merged.get("properties", {})


def infer_log_schema(
    bodies: Iterable[str],
    schema_name: str = DEFAULT_SCHEMA_NAME,
    expand_json_strings: bool = True,
) -> InferredLogSchema:
    """
    Recover the shape of a log stream from a sample of its bodies.

    Only the records matching the dominant format contribute fields, so a
    stray unparsable line cannot flatten an otherwise structured schema. A
    sample with no recognisable structure yields a single `message` string
    field, which keeps the entity honest about what was found.
    """
    sampled = list(bodies)
    records = parse_log_bodies(sampled)
    log_format = _dominant_format(records)
    matching = [
        record for record in records if record.format is log_format and record.fields
    ]
    if log_format is LogFormat.RAW or not matching:
        log_format = LogFormat.RAW
        properties = {RAW_MESSAGE_FIELD: {"type": "string"}}
    else:
        properties = _properties_for(matching, expand_json_strings)

    return InferredLogSchema(
        format=log_format,
        json_schema={
            "$schema": JSON_SCHEMA_DRAFT,
            "title": schema_name,
            "type": "object",
            "properties": properties,
        },
        sampled_records=len(sampled),
        parsed_records=len(matching),
        patterns=sorted({record.pattern for record in matching if record.pattern}),
    )
