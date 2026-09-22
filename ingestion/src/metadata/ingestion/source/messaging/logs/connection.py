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
Connection helpers shared by log connectors.

Custom connectors are resolved by `import_connection_fn`, which looks for
module-level `get_connection` / `test_connection` next to the class named in
`sourcePythonClass`. Every log connector therefore repeats the same wiring;
these helpers keep that boilerplate down to a few lines.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Sequence

from metadata.generated.schema.entity.automations.workflow import (
    Workflow as AutomationWorkflow,
)
from metadata.generated.schema.entity.automations.workflow import WorkflowStatus
from metadata.generated.schema.entity.services.connections.testConnectionResult import (
    StatusType,
    TestConnectionResult,
    TestConnectionStepResult,
)
from metadata.ingestion.connections.test_connections import SourceConnectionException
from metadata.ingestion.ometa.ometa_api import OpenMetadata


@dataclass(frozen=True)
class ConnectionCheck:
    """
    One named step of a connector's test-connection routine.

    `run` receives the client built by the connector's `get_connection` and
    raises to signal failure. A non-mandatory step that fails is reported
    but does not fail the connection.
    """

    name: str
    mandatory: bool
    run: Callable[[Any], None]


def get_connection_options(connection: Any) -> Dict[str, str]:
    """Flatten `connectionOptions` into a plain dict, empty when unset."""
    options = getattr(connection, "connectionOptions", None)
    return dict(options.root) if options and options.root else {}


def require_option(options: Dict[str, str], key: str, purpose: str) -> str:
    """Read a mandatory connection option or explain what it is needed for."""
    value = options.get(key)
    if not value:
        raise SourceConnectionException(
            f"connectionOptions.{key} is required to {purpose}"
        )
    return value


def run_connection_checks(
    metadata: OpenMetadata,
    client: Any,
    checks: Sequence[ConnectionCheck],
    automation_workflow: Optional[AutomationWorkflow] = None,
) -> TestConnectionResult:
    """
    Execute the checks and report them as a `TestConnectionResult`.

    Built directly instead of through `test_connection_steps()`: that helper
    resolves a `<ServiceType>.testConnectionDefinition` entity server-side,
    which is not guaranteed to be seeded for custom connectors.
    """
    steps = []
    for check in checks:
        try:
            check.run(client)
            steps.append(
                TestConnectionStepResult(
                    name=check.name, mandatory=check.mandatory, passed=True
                )
            )
        except Exception as exc:
            steps.append(
                TestConnectionStepResult(
                    name=check.name,
                    mandatory=check.mandatory,
                    passed=False,
                    errorLog=str(exc),
                )
            )

    failed_mandatory = any(not step.passed and step.mandatory for step in steps)
    result = TestConnectionResult(
        status=StatusType.Failed if failed_mandatory else StatusType.Successful,
        steps=steps,
    )
    if automation_workflow:
        metadata.patch_automation_workflow_response(
            automation_workflow,
            result,
            (
                WorkflowStatus.Failed
                if result.status == StatusType.Failed
                else WorkflowStatus.Successful
            ),
        )
    return result
