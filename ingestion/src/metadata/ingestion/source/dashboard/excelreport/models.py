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
Plain data structures used to hold the content parsed out of a
QT.B14.QLDL.MB06.1 "YCPT_BAOCAO" style report-request workbook.

These models have no dependency on the generated OpenMetadata schema
classes on purpose: they are the boundary between the Excel parsing
logic (`parser.py`, unit-testable on its own) and the ingestion source
(`metadata.py`, which turns them into Create*Request objects).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional  # noqa: UP035

DEFAULT_CHART_GROUP = "Chi tiết"


@dataclass
class ReportOverview:
    """One row of the 'PL3.1 Tổng quan báo cáo' sheet - describes one Dashboard."""

    report_name: str
    report_code: Optional[str] = None  # noqa: UP045
    folder: Optional[str] = None  # noqa: UP045
    link: Optional[str] = None  # noqa: UP045
    report_type: Optional[str] = None  # noqa: UP045
    requester_unit_l1: Optional[str] = None  # noqa: UP045
    requester_unit_l2: Optional[str] = None  # noqa: UP045
    business_contact: Optional[str] = None  # noqa: UP045
    data_unit: Optional[str] = None  # noqa: UP045
    data_contact: Optional[str] = None  # noqa: UP045
    purpose: Optional[str] = None  # noqa: UP045
    using_unit: Optional[str] = None  # noqa: UP045
    using_title: Optional[str] = None  # noqa: UP045
    data_permission: Optional[str] = None  # noqa: UP045
    channel: Optional[str] = None  # noqa: UP045
    params: Optional[str] = None  # noqa: UP045
    frequency: Optional[str] = None  # noqa: UP045
    date_range: Optional[str] = None  # noqa: UP045
    history_required: Optional[str] = None  # noqa: UP045
    daily_time: Optional[str] = None  # noqa: UP045
    sensitive_data: Optional[str] = None  # noqa: UP045


@dataclass
class MappingRow:
    """One indicator/parameter row of the mapping sheet (PL3.3/PL3.4)."""

    indicator_name: str
    group_label: Optional[str] = None  # noqa: UP045
    indicator_type: Optional[str] = None  # noqa: UP045
    sheet_name: Optional[str] = None  # noqa: UP045
    section_name: Optional[str] = None  # noqa: UP045
    meaning: Optional[str] = None  # noqa: UP045
    calc_logic: Optional[str] = None  # noqa: UP045
    unit: Optional[str] = None  # noqa: UP045
    source_system: Optional[str] = None  # noqa: UP045
    input_ref: Optional[str] = None  # noqa: UP045
    source_schema: Optional[str] = None  # noqa: UP045
    source_table: Optional[str] = None  # noqa: UP045
    source_field: Optional[str] = None  # noqa: UP045
    mapping_rule: Optional[str] = None  # noqa: UP045
    rule_reconcile: Optional[str] = None  # noqa: UP045
    metadata_note: Optional[str] = None  # noqa: UP045
    note: Optional[str] = None  # noqa: UP045

    @property
    def has_source_reference(self) -> bool:
        return bool(self.source_schema and self.source_table and self.source_field)

    @property
    def chart_group(self) -> str:
        """Which Chart this indicator belongs to.

        Grouped by 'Tên Sheet' - the report's actual pages/tabs (e.g. Sheet1 =
        Top10, Sheet2 = Chi tiết) - since that's the real distinguishing unit
        of a dashboard's components. 'Tên mục/biểu đồ' is descriptive content
        within that chart, not a separate split, and only falls back to being
        the group when a report has no sheet breakdown at all.
        """
        return self.sheet_name or self.section_name or DEFAULT_CHART_GROUP


@dataclass
class ParsedDashboard:
    """Everything extracted from one workbook that belongs to a single Dashboard."""

    overview: ReportOverview
    source_file: Path
    mapping_rows: List[MappingRow] = field(default_factory=list)  # noqa: UP006
    mockup_markdown: Optional[str] = None  # noqa: UP045
    impact_markdown: Optional[str] = None  # noqa: UP045

    @property
    def dashboard_key(self) -> str:
        return self.overview.report_code or self.overview.report_name

    def chart_groups(self) -> List[str]:  # noqa: UP006
        seen: List[str] = []  # noqa: UP006
        for row in self.mapping_rows:
            if row.chart_group not in seen:
                seen.append(row.chart_group)
        return seen

    def rows_for_chart_group(self, chart_group: str) -> List[MappingRow]:  # noqa: UP006
        return [row for row in self.mapping_rows if row.chart_group == chart_group]
