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
Split a raw log body into named fields.

Formats are tried in decreasing order of confidence: JSON, then logfmt,
then a small set of well-known plaintext layouts. A body that matches none
of them is returned as `RAW` rather than force-fitted, so a stream of
free-form text never produces an invented schema.
"""

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from metadata.parsers.logs.models import LogFormat, ParsedLogRecord

LOG_LEVELS = "TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|SEVERE|FATAL|CRITICAL"

ISO_TIMESTAMP = (
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?(?:Z|[+-]\d{2}:?\d{2})?"
)

_ISO_TIMESTAMP_RE = re.compile(rf"^{ISO_TIMESTAMP}$")

# A logger name must look qualified (a.b, a/b) before we accept it as one.
# Without that, "user: not found" would be read as logger "user".
_QUALIFIED_LOGGER = r"[\w$]+(?:[./][\w$]+)+"

_LOGFMT_PAIR_RE = re.compile(r'([A-Za-z_][\w.\-]*)=("(?:[^"\\]|\\.)*"|[^\s"]*)')

# Share of a line that logfmt pairs must cover before the line is called
# logfmt. Below it, the pairs are incidental (a `key=value` inside prose).
_LOGFMT_COVERAGE_THRESHOLD = 0.6
_LOGFMT_MIN_PAIRS = 2

# Fields kept verbatim: coercing them would turn an all-numeric message or a
# numeric-looking timestamp into a number and destabilise the inferred type.
_NEVER_COERCED = frozenset(
    {"message", "timestamp", "request", "referrer", "user_agent"}
)

_NULL_LITERALS = frozenset({"", "-", "null", "nil", "none"})

_PLAINTEXT_PATTERNS: Tuple[Tuple[str, "re.Pattern"], ...] = (
    (
        # Common / combined log format:
        # 10.0.0.1 - - [06/Aug/2026:10:11:12 +0700] "GET / HTTP/1.1" 200 1234
        "clf",
        re.compile(
            r"^(?P<client_ip>\S+)\s+(?P<ident>\S+)\s+(?P<user>\S+)\s+"
            r"\[(?P<timestamp>[^\]]+)\]\s+"
            r'"(?P<request>[^"]*)"\s+(?P<status>\d{3})\s+(?P<bytes_sent>\d+|-)'
            r'(?:\s+"(?P<referrer>[^"]*)"\s+"(?P<user_agent>[^"]*)")?\s*$'
        ),
    ),
    (
        # Bracketed: [2026-08-06T10:11:12Z] [ERROR] disk full
        "bracketed",
        re.compile(
            rf"^\[(?P<timestamp>{ISO_TIMESTAMP})\]\s*"
            rf"\[(?P<level>{LOG_LEVELS})\]\s*(?P<message>.*)$",
            re.IGNORECASE,
        ),
    ),
    (
        # Application log:
        # 2026-08-06 10:11:12,123 [main] INFO com.foo.Bar - started
        "timestamped",
        re.compile(
            rf"^(?P<timestamp>{ISO_TIMESTAMP})\s+"
            rf"(?:\[(?P<thread>[^\]]*)\]\s+)?"
            rf"(?P<level>{LOG_LEVELS})\s+"
            rf"(?:(?P<logger>{_QUALIFIED_LOGGER})\s*[-:]\s+)?"
            r"(?P<message>.*)$",
            re.IGNORECASE,
        ),
    ),
    (
        # Syslog: Aug  6 10:11:12 host sshd[123]: accepted publickey
        "syslog",
        re.compile(
            r"^(?P<timestamp>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+"
            r"(?P<host>\S+)\s+(?P<process>[\w.\-/]+)(?:\[(?P<pid>\d+)\])?:\s+"
            r"(?P<message>.*)$"
        ),
    ),
)


def _coerce_scalar(value: str) -> Any:
    """Read a stringly-typed value back into the scalar it represents."""
    stripped = value.strip()
    if stripped.lower() in _NULL_LITERALS:
        return None
    if stripped.lower() in {"true", "false"}:
        return stripped.lower() == "true"
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        return stripped


def _coerce_field(key: str, value: Optional[str]) -> Any:
    result = None
    if value is not None:
        result = value if key in _NEVER_COERCED else _coerce_scalar(value)
    return result


def _unquote(value: str) -> str:
    unquoted = value
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        try:
            unquoted = json.loads(value)
        except ValueError:
            unquoted = value[1:-1]
    return unquoted


def parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    """
    Read a JSON object out of `text`, or return None.

    Only an object counts: a bare scalar or array is not a log record, and
    accepting one would invent a schema out of an ordinary string.
    """
    result = None
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            loaded = json.loads(stripped)
            result = loaded if isinstance(loaded, dict) else None
        except ValueError:
            result = None
    return result


def _parse_json(body: str) -> Optional[Dict[str, Any]]:
    """
    Read a JSON object, tolerating a non-JSON prefix.

    Container runtimes routinely prepend a timestamp to an application's
    JSON line, so a strict parse failure is retried from the first brace.
    """
    fields = parse_json_object(body)
    if fields is None:
        start = body.find("{")
        end = body.rfind("}")
        if start != -1 and end > start:
            fields = parse_json_object(body[start : end + 1])
    return fields


def _parse_logfmt(body: str) -> Optional[Dict[str, Any]]:
    matches = [
        match for match in _LOGFMT_PAIR_RE.finditer(body) if match.group(2) != ""
    ]
    fields = None
    if len(matches) >= _LOGFMT_MIN_PAIRS:
        covered = sum(match.end() - match.start() for match in matches)
        stripped_length = len(body.strip())
        if stripped_length and covered / stripped_length >= _LOGFMT_COVERAGE_THRESHOLD:
            fields = {
                match.group(1): _coerce_field(match.group(1), _unquote(match.group(2)))
                for match in matches
            }
    return fields


def _parse_plaintext(body: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    result = None
    for name, pattern in _PLAINTEXT_PATTERNS:
        match = pattern.match(body.strip())
        if match:
            fields = {
                key: _coerce_field(key, value)
                for key, value in match.groupdict().items()
                if value is not None
            }
            result = (name, fields)
            break
    return result


def parse_log_body(body: str) -> ParsedLogRecord:
    """
    Split one log body into fields, detecting its format on the way.

    Never raises: an unrecognised body comes back as `LogFormat.RAW` with no
    fields, which the schema inference reports rather than guesses around.
    """
    record = ParsedLogRecord(format=LogFormat.RAW, body=body)
    if body and body.strip():
        json_fields = _parse_json(body)
        logfmt_fields = None if json_fields is not None else _parse_logfmt(body)
        plaintext = (
            None
            if json_fields is not None or logfmt_fields is not None
            else _parse_plaintext(body)
        )
        if json_fields is not None:
            record = ParsedLogRecord(
                format=LogFormat.JSON, fields=json_fields, body=body
            )
        elif logfmt_fields is not None:
            record = ParsedLogRecord(
                format=LogFormat.LOGFMT, fields=logfmt_fields, body=body
            )
        elif plaintext is not None:
            pattern_name, fields = plaintext
            record = ParsedLogRecord(
                format=LogFormat.PLAINTEXT,
                fields=fields,
                body=body,
                pattern=pattern_name,
            )
    return record


def parse_log_bodies(bodies: Iterable[str]) -> List[ParsedLogRecord]:
    """Parse every sampled body, preserving order."""
    return [parse_log_body(body) for body in bodies]


def detect_log_format(body: str) -> LogFormat:
    """Format of a single body, without keeping the parsed fields."""
    return parse_log_body(body).format


def is_timestamp_like(value: str) -> bool:
    """Whether a string value looks like an ISO-8601 instant."""
    return bool(_ISO_TIMESTAMP_RE.match(value.strip()))
