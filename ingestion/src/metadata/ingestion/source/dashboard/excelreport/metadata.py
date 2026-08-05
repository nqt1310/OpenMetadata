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
Custom Dashboard source that turns QT.B14.QLDL.MB06.1 "YCPT_BAOCAO" style
report-request workbooks into Dashboard / Chart metadata and best-effort
lineage to the source tables named in their mapping sheet.

This is wired up as a `CustomDashboardConnection` (`sourcePythonClass`
pointing at `ExcelReportSource` below), not a first-class connector: no
JSON schema / Java / UI change is required to use it. See
`connectionOptions`:
  * directoryPath (required): folder to scan for workbooks
  * filePattern (optional, default "*.xlsx")

Note on lineage granularity: `DashboardDataModel.dataModelType` is a
closed enum of vendor types (Tableau, PowerBI, ...) with no generic
value, so this source cannot create a DashboardDataModel to carry true
column-to-column lineage without a core schema change. It resolves
table-level lineage instead (Table -> Dashboard, Table -> Chart) and
keeps the full field-level mapping (source schema/table/field, mapping
rule, reconcile rule) as Markdown on the Chart/Dashboard description.
"""

import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional  # noqa: UP035

from pydantic import ValidationError

from metadata.generated.schema.api.data.createChart import CreateChartRequest
from metadata.generated.schema.api.data.createDashboard import CreateDashboardRequest
from metadata.generated.schema.api.lineage.addLineage import AddLineageRequest
from metadata.generated.schema.entity.automations.workflow import (
    Workflow as AutomationWorkflow,
)
from metadata.generated.schema.entity.automations.workflow import WorkflowStatus
from metadata.generated.schema.entity.data.chart import Chart, ChartType
from metadata.generated.schema.entity.data.dashboard import Dashboard
from metadata.generated.schema.entity.data.table import Table
from metadata.generated.schema.entity.services.connections.dashboard.customDashboardConnection import (
    CustomDashboardConnection,
)
from metadata.generated.schema.entity.services.connections.metadata.openMetadataConnection import (
    OpenMetadataConnection,
)
from metadata.generated.schema.entity.services.connections.testConnectionResult import (
    StatusType,
    TestConnectionResult,
    TestConnectionStepResult,
)
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
    SourceUrl,
)
from metadata.generated.schema.type.entityReferenceList import EntityReferenceList
from metadata.ingestion.api.models import Either
from metadata.ingestion.api.steps import InvalidSourceException
from metadata.ingestion.connections.test_connections import SourceConnectionException
from metadata.ingestion.ometa.ometa_api import OpenMetadata
from metadata.ingestion.source.dashboard.dashboard_service import DashboardServiceSource
from metadata.ingestion.source.dashboard.excelreport import parser as excel_parser
from metadata.ingestion.source.dashboard.excelreport.models import ParsedDashboard
from metadata.utils import fqn
from metadata.utils.constants import THREE_MIN
from metadata.utils.filters import filter_by_chart
from metadata.utils.fqn import build_es_fqn_search_string
from metadata.utils.logger import ingestion_logger

logger = ingestion_logger()

DIRECTORY_PATH_KEY = "directoryPath"
FILE_PATTERN_KEY = "filePattern"
DEFAULT_FILE_PATTERN = "*.xlsx"


@dataclass
class ExcelReportClient:
    directory: Path
    file_pattern: str

    def list_files(self) -> List[Path]:  # noqa: UP006
        return sorted(path for path in self.directory.glob(self.file_pattern) if path.is_file())


def get_connection(connection: CustomDashboardConnection) -> ExcelReportClient:
    options = connection.connectionOptions.root if connection.connectionOptions else {}
    directory = options.get(DIRECTORY_PATH_KEY)
    if not directory:
        raise SourceConnectionException(
            f"connectionOptions.{DIRECTORY_PATH_KEY} is required to locate the YCPT_BAOCAO workbooks"
        )
    directory_path = Path(directory)
    if not directory_path.is_dir():
        raise SourceConnectionException(f"'{directory}' is not a directory OpenMetadata can read")
    file_pattern = options.get(FILE_PATTERN_KEY) or DEFAULT_FILE_PATTERN
    return ExcelReportClient(directory=directory_path, file_pattern=file_pattern)


def _require_report_files(client: ExcelReportClient) -> None:
    if not client.list_files():
        raise SourceConnectionException(f"No files matching '{client.file_pattern}' found in '{client.directory}'")


def _read_sample_workbook(client: ExcelReportClient) -> None:
    files = client.list_files()
    if files:
        excel_parser.parse_workbook(files[0])


def test_connection(
    metadata: OpenMetadata,
    client: ExcelReportClient,
    service_connection: CustomDashboardConnection,
    automation_workflow: Optional[AutomationWorkflow] = None,  # noqa: UP045
    timeout_seconds: Optional[int] = THREE_MIN,  # noqa: UP045
) -> TestConnectionResult:
    """
    Test connection. This can be executed either as part of a metadata
    workflow or during an Automation Workflow.

    Builds the TestConnectionResult directly instead of going through the
    generic test_connection_steps() helper: that helper looks up a
    "CustomDashboard.testConnectionDefinition" entity server-side, which
    isn't guaranteed to be seeded on every instance, and its legacy dispatch
    path is not stable across OpenMetadata versions (observed a signature
    mismatch between this repo's dev branch and a released 1.12.x server).
    """
    checks = (
        ("ListReportFiles", True, _require_report_files),
        ("ReadSampleWorkbook", False, _read_sample_workbook),
    )
    steps = []
    for name, mandatory, check in checks:
        try:
            check(client)
            steps.append(TestConnectionStepResult(name=name, mandatory=mandatory, passed=True))
        except Exception as exc:
            steps.append(
                TestConnectionStepResult(  # pyright: ignore[reportCallIssue]
                    name=name,
                    mandatory=mandatory,
                    passed=False,
                    errorLog=str(exc),
                )
            )

    result = TestConnectionResult(
        status=(
            StatusType.Failed if any(not step.passed and step.mandatory for step in steps) else StatusType.Successful
        ),
        steps=steps,
    )
    if automation_workflow:
        metadata.patch_automation_workflow_response(
            automation_workflow,
            result,
            WorkflowStatus.Failed if result.status == StatusType.Failed else WorkflowStatus.Successful,
        )
    return result


class ExcelReportSource(DashboardServiceSource):
    """
    Reads YCPT_BAOCAO report-request workbooks from a local/mounted folder and
    creates one Dashboard per report row, one Chart per "Tên Sheet" (falling
    back to "Tên mục/biểu đồ" when a report has no sheet breakdown), and
    table-level lineage to the source tables named in the mapping sheet.
    """

    config: WorkflowSource
    metadata_config: OpenMetadataConnection

    @classmethod
    def create(cls, config_dict, metadata: OpenMetadata, pipeline_name: Optional[str] = None):  # noqa: UP045
        config = WorkflowSource.model_validate(config_dict)
        connection: CustomDashboardConnection = config.serviceConnection.root.config
        if not isinstance(connection, CustomDashboardConnection):
            raise InvalidSourceException(f"Expected CustomDashboardConnection, but got {connection}")
        return cls(config, metadata)

    def get_dashboards_list(self) -> Optional[List[ParsedDashboard]]:  # noqa: UP006, UP045
        dashboards: List[ParsedDashboard] = []  # noqa: UP006
        for path in self.client.list_files():
            try:
                dashboards.extend(excel_parser.parse_workbook(path))
            except Exception as exc:
                logger.warning(f"Error parsing report-request workbook {path}: {exc}")
                logger.debug(traceback.format_exc())
        return dashboards

    def get_dashboard_name(self, dashboard: ParsedDashboard) -> str:
        return dashboard.dashboard_key

    def get_dashboard_details(self, dashboard: ParsedDashboard) -> ParsedDashboard:
        return dashboard

    def get_project_name(self, dashboard_details: ParsedDashboard) -> Optional[str]:  # noqa: UP045
        return dashboard_details.overview.requester_unit_l1

    def get_owner_ref(self, dashboard_details: ParsedDashboard) -> Optional[EntityReferenceList]:  # noqa: UP045
        contact = dashboard_details.overview.business_contact
        if contact and "@" in contact:
            try:
                owner_ref = self.metadata.get_reference_by_email(contact.strip())
                if owner_ref:
                    return owner_ref
            except Exception as exc:
                logger.debug(f"Could not resolve owner from business contact '{contact}': {exc}")
        return None

    def _chart_name(self, dashboard_details: ParsedDashboard, chart_group: str) -> str:
        return f"{dashboard_details.dashboard_key}_{excel_parser.slugify(chart_group)}"

    def _register_chart(self, chart_request: CreateChartRequest) -> None:
        """Marks the chart as scanned, so mark_charts_as_deleted can find stale ones.

        Older DashboardServiceSource releases (e.g. 1.12.x, which this connector
        has also been run against) have neither the register_record_chart
        convenience method nor the chart_source_state set it updates - that
        stale-chart cleanup feature simply doesn't exist yet there. Degrade to
        a no-op in that case rather than fail dashboard/chart creation over a
        bookkeeping detail for an optional cleanup step.
        """
        if hasattr(self, "register_record_chart"):
            self.register_record_chart(chart_request=chart_request)
        elif hasattr(self, "chart_source_state"):
            chart_fqn = fqn.build(
                self.metadata,
                entity_type=Chart,
                service_name=chart_request.service.root,
                chart_name=chart_request.name.root,
            )
            self.chart_source_state.add(chart_fqn)

    def yield_dashboard(self, dashboard_details: ParsedDashboard) -> Iterable[Either[CreateDashboardRequest]]:
        dashboard_name = dashboard_details.dashboard_key
        try:
            source_url = dashboard_details.overview.link
            dashboard_request = CreateDashboardRequest(
                name=EntityName(dashboard_name),
                displayName=dashboard_details.overview.report_name,
                description=Markdown(excel_parser.render_dashboard_description(dashboard_details)),
                sourceUrl=(SourceUrl(source_url) if source_url and "://" in source_url else None),
                charts=[
                    FullyQualifiedEntityName(
                        fqn.build(
                            self.metadata,
                            entity_type=Chart,
                            service_name=self.context.get().dashboard_service,
                            chart_name=chart,
                        )
                    )
                    for chart in self.context.get().charts or []
                ],
                service=self.context.get().dashboard_service,
                owners=self.get_owner_ref(dashboard_details=dashboard_details),
            )
            yield Either(right=dashboard_request)
            self.register_record(dashboard_request=dashboard_request)
        except ValidationError as err:
            yield Either(
                left=StackTraceError(
                    name=dashboard_name,
                    error=f"Error building pydantic model for {dashboard_name} - {err}",
                    stackTrace=traceback.format_exc(),
                )
            )
        except Exception as err:
            yield Either(
                left=StackTraceError(
                    name=dashboard_name,
                    error=f"Error creating dashboard {dashboard_name} - {err}",
                    stackTrace=traceback.format_exc(),
                )
            )

    def yield_dashboard_chart(self, dashboard_details: ParsedDashboard) -> Iterable[Either[CreateChartRequest]]:
        for chart_group in dashboard_details.chart_groups():
            chart_name = self._chart_name(dashboard_details, chart_group)
            if filter_by_chart(self.source_config.chartFilterPattern, chart_name):
                self.status.filter(chart_name, "Chart Pattern not allowed")
                continue
            try:
                rows = dashboard_details.rows_for_chart_group(chart_group)
                chart_request = CreateChartRequest(
                    name=EntityName(chart_name),
                    displayName=chart_group,
                    description=Markdown(excel_parser.render_chart_description(rows)),
                    chartType=ChartType.Other,
                    service=self.context.get().dashboard_service,
                )
                yield Either(right=chart_request)
                self._register_chart(chart_request)
            except Exception as exc:
                yield Either(
                    left=StackTraceError(
                        name=chart_name,
                        error=f"Error creating chart [{chart_name}]: {exc}",
                        stackTrace=traceback.format_exc(),
                    )
                )

    def _resolve_dashboard_entity(self, dashboard_details: ParsedDashboard) -> Optional[Dashboard]:  # noqa: UP045
        dashboard_fqn = fqn.build(
            self.metadata,
            entity_type=Dashboard,
            service_name=self.context.get().dashboard_service,
            dashboard_name=dashboard_details.dashboard_key,
        )
        return self.metadata.get_by_name(entity=Dashboard, fqn=dashboard_fqn)

    def _resolve_chart_entity(self, dashboard_details: ParsedDashboard, chart_group: str) -> Chart | None:
        chart_fqn = fqn.build(
            self.metadata,
            entity_type=Chart,
            service_name=self.context.get().dashboard_service,
            chart_name=self._chart_name(dashboard_details, chart_group),
        )
        return self.metadata.get_by_name(entity=Chart, fqn=chart_fqn)

    def _find_source_tables(
        self,
        schema_name: Optional[str],  # noqa: UP045
        table_name: Optional[str],  # noqa: UP045
        prefix_service_name: Optional[str],  # noqa: UP045
        prefix_database_name: Optional[str],  # noqa: UP045
        prefix_schema_name: Optional[str],  # noqa: UP045
        prefix_table_name: Optional[str],  # noqa: UP045
    ) -> List[Table]:  # noqa: UP006
        if prefix_schema_name and schema_name and prefix_schema_name.lower() != schema_name.lower():
            return []
        if prefix_table_name and table_name and prefix_table_name.lower() != table_name.lower():
            return []
        try:
            fqn_search_string = build_es_fqn_search_string(
                database_name=prefix_database_name,
                schema_name=prefix_schema_name or schema_name,
                service_name=prefix_service_name or "*",
                table_name=prefix_table_name or table_name,
            )
            from_entities = self.metadata.search_in_any_service(
                entity_type=Table,
                fqn_search_string=fqn_search_string,
                fetch_multiple_entities=True,
            )
        except Exception as exc:
            logger.debug(f"Could not resolve source table {schema_name}.{table_name}: {exc}")
            return []
        return [from_entities] if isinstance(from_entities, Table) else list(from_entities or [])

    def yield_dashboard_lineage_details(
        self,
        dashboard_details: ParsedDashboard,
        db_service_prefix: Optional[str] = None,  # noqa: UP045
    ) -> Iterable[Either[AddLineageRequest]]:
        (
            prefix_service_name,
            prefix_database_name,
            prefix_schema_name,
            prefix_table_name,
        ) = self.parse_db_service_prefix(db_service_prefix)

        dashboard_entity = self._resolve_dashboard_entity(dashboard_details)
        dashboard_tables_seen = set()

        for chart_group in dashboard_details.chart_groups():
            chart_entity = self._resolve_chart_entity(dashboard_details, chart_group)
            table_keys = {
                (row.source_schema, row.source_table)
                for row in dashboard_details.rows_for_chart_group(chart_group)
                if row.has_source_reference
            }
            for schema_name, table_name in table_keys:
                from_tables = self._find_source_tables(
                    schema_name,
                    table_name,
                    prefix_service_name,
                    prefix_database_name,
                    prefix_schema_name,
                    prefix_table_name,
                )
                if not from_tables:
                    logger.debug(
                        f"No cataloged table found for {schema_name}.{table_name}, "
                        "keeping the mapping as description-only"
                    )
                for from_entity in from_tables:
                    if dashboard_entity and (schema_name, table_name) not in dashboard_tables_seen:
                        lineage = self._get_add_lineage_request(to_entity=dashboard_entity, from_entity=from_entity)
                        if lineage:
                            yield lineage
                    if chart_entity:
                        lineage = self._get_add_lineage_request(to_entity=chart_entity, from_entity=from_entity)
                        if lineage:
                            yield lineage
                dashboard_tables_seen.add((schema_name, table_name))
