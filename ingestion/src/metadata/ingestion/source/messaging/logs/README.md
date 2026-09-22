# Log connectors

Scaffolding for ingesting log streams into OpenMetadata as Topics.

Log backends do not publish a schema, so a log connector has to recover one
from the payload. That work — sampling, format detection, parsing, schema
inference, entity creation — lives in `BaseLogSource` and
`metadata.parsers.logs`. A concrete connector only answers two questions:

1. **Which streams exist?** (`get_log_streams`)
2. **How do I read a few lines off one?** (`fetch_log_sample`)

Everything else is inherited.

## What ends up in OpenMetadata

One log stream becomes one **Topic**:

| Topic field | Comes from |
| --- | --- |
| `name`, `displayName`, `description` | the `LogStream` the connector yielded |
| `messageSchema.schemaText` | JSON Schema inferred from the sampled bodies |
| `messageSchema.schemaFields` | that schema, run through the existing JSON Schema field parser |
| `messageSchema.schemaType` | always `JSON` — the inferred schema is a JSON Schema regardless of the log's own encoding |
| `topicConfig` | the stream's `attributes`, plus `logFormat`, `logPatterns`, `sampledRecords`, `parsedRecords` |
| sample data | the sampled bodies verbatim, when `generateSampleData` is on |

`parsedRecords` below `sampledRecords` means the stream is mixed and the
schema only describes the dominant format. `logFormat: raw` means nothing
was recognised and the schema is a single `message` string — that is
reported, never papered over.

## Supported body formats

Detected per line, in this order, by `metadata.parsers.logs`:

| Format | Example |
| --- | --- |
| `json` | `{"ts":"2026-08-06T10:11:12Z","level":"ERROR","msg":"boom"}` |
| `logfmt` | `ts=2026-08-06T10:11:12Z level=error msg="disk full" retries=3` |
| `plaintext` / `timestamped` | `2026-08-06 10:11:12,123 [main] INFO com.foo.Bar - started` |
| `plaintext` / `bracketed` | `[2026-08-06T10:11:12Z] [ERROR] disk full` |
| `plaintext` / `syslog` | `Aug  6 10:11:12 host sshd[123]: accepted publickey` |
| `plaintext` / `clf` | `10.0.0.1 - - [06/Aug/2026:10:11:12 +0700] "GET / HTTP/1.1" 200 1234` |
| `raw` | anything else — kept as one `message` field |

JSON detection tolerates a non-JSON prefix, so a container runtime that
prepends its own timestamp to an application's JSON line still parses.

### Payloads carried as JSON strings

Log envelopes routinely ship the interesting part as an escaped JSON string
in a `body` / `payload` field:

```json
{"application": "svc", "body": "{\"result\":1,\"ward_code\":\"00385\"}"}
```

The transport records that as plain text, so the structure is invisible to
the backend storing it. The inference expands such a string into its own
fields and marks the node `contentMediaType: application/json`, so the
catalogue shows `body.result` and `body.ward_code` while the schema stays
honest that the stored type is text. Pass `expand_json_strings=False` to
`infer_log_schema` to turn this off.

To teach every log connector a new plaintext layout, add a pattern to
`_PLAINTEXT_PATTERNS` in `metadata/parsers/logs/formats.py` — not to a
connector.

## Writing a new log connector

Copy `metadata/ingestion/source/messaging/filelog/metadata.py`; it is the
reference implementation and deliberately small. Four things are required.

### 1. A client and a module-level `get_connection`

Custom connectors are resolved by `import_connection_fn`, which looks for
`get_connection` and `test_connection` **in the same module** as the class
named in `sourcePythonClass`. Both must exist at module level.

```python
def get_connection(connection: CustomMessagingConnection) -> MyLogClient:
    options = get_connection_options(connection)
    endpoint = require_option(options, "endpoint", "reach the log backend")
    return MyLogClient(endpoint=endpoint)
```

### 2. A module-level `test_connection`

```python
def test_connection(
    metadata: OpenMetadata,
    client: MyLogClient,
    service_connection: CustomMessagingConnection,
    automation_workflow: Optional[AutomationWorkflow] = None,
    timeout_seconds: Optional[int] = THREE_MIN,
) -> TestConnectionResult:
    checks = (
        ConnectionCheck(name="ListLogStreams", mandatory=True, run=_require_streams),
        ConnectionCheck(name="ReadSampleLines", mandatory=False, run=_read_sample),
    )
    return run_connection_checks(metadata, client, checks, automation_workflow)
```

`run_connection_checks` builds the result directly instead of going through
`test_connection_steps()`, which resolves a server-side
`testConnectionDefinition` that is not guaranteed to be seeded for custom
connectors.

### 3. The source class

```python
class MyLogSource(BaseLogSource):
    @classmethod
    def create(cls, config_dict, metadata, pipeline_name=None) -> "MyLogSource":
        config = WorkflowSource.model_validate(config_dict)
        connection = config.serviceConnection.root.config
        if not isinstance(connection, CustomMessagingConnection):
            raise InvalidSourceException(
                f"Expected CustomMessagingConnection, but got {connection}"
            )
        return cls(config, metadata)

    def get_log_streams(self) -> Iterable[LogStream]:
        for group in self.connection.list_groups():
            yield LogStream(
                name=group.name,
                attributes={"region": group.region},
                handle=group,          # handed back to fetch_log_sample untouched
            )

    def fetch_log_sample(self, stream: LogStream, limit: int) -> Iterator[str]:
        yield from self.connection.tail(stream.handle, limit)
```

`self.connection` is whatever `get_connection` returned.
`self.connection_options` exposes the raw `connectionOptions` dict.

Yield bodies as the source stores them. Decoding and de-framing belong in
`fetch_log_sample`; parsing does not — the base class does that. Returning
fewer than `limit` lines is fine; returning none produces a topic with a
placeholder schema rather than an error.

### 4. The workflow config

```yaml
source:
  type: custommessaging
  serviceName: my-logs
  serviceConnection:
    config:
      type: CustomMessaging
      sourcePythonClass: metadata.ingestion.source.messaging.mylogs.metadata.MyLogSource
      connectionOptions:
        endpoint: https://logs.internal
        sampleSize: "10"
  sourceConfig:
    config:
      type: MessagingMetadata
      generateSampleData: true
```

`sampleSize` (default 10, clamped to 1–100) is read by the base class for
every log connector, so a new connector does not re-implement it.

## Conventions to keep

- **Nothing connector-specific in `logs/` or `parsers/logs/`.** A backend
  quirk belongs in that backend's module. A log *format* belongs in
  `parsers/logs/formats.py`, where every connector benefits.
- **`fetch_log_sample` must be cheap.** It runs once per stream on every
  ingestion. Read a head or a tail, never a whole file.
- **Never invent structure.** If a body does not match a known format it is
  `raw`, and the topic says so.
