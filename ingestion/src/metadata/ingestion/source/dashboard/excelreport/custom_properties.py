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
Report-request fields as Dashboard custom properties.

The YCPT_BAOCAO workbook carries a fixed set of governance fields - who
asked for the report, which unit may read it, how often it runs, whether it
holds sensitive data. Rendering them into the description made them
readable but not usable: they could not be filtered, searched or reported
on. Declaring them as custom properties puts each one in its own typed
field instead.

Registration is idempotent (`create_or_update`) and needs an admin-capable
ingestion bot, since it edits the `dashboard` Type. When it fails the
connector falls back to the Markdown description, so a restricted bot
degrades instead of losing the data.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from metadata.generated.schema.api.data.createCustomProperty import (
    CreateCustomPropertyRequest,
)
from metadata.generated.schema.entity.data.dashboard import Dashboard
from metadata.generated.schema.type.basic import EntityName, Markdown
from metadata.ingestion.models.custom_properties import (
    CustomPropertyDataTypes,
    OMetaCustomProperties,
)
from metadata.ingestion.ometa.ometa_api import OpenMetadata
from metadata.ingestion.source.dashboard.excelreport.models import ParsedDashboard
from metadata.utils.logger import ingestion_logger

logger = ingestion_logger()


@dataclass(frozen=True)
class ReportProperty:
    """
    One workbook field promoted to a Dashboard custom property.

    `source` is a dotted path read off `ParsedDashboard`, so the mapping
    stays declarative and mirrors the workbook layout.
    """

    name: str
    display_name: str
    source: str
    data_type: CustomPropertyDataTypes = CustomPropertyDataTypes.STRING


REPORT_PROPERTIES: Tuple[ReportProperty, ...] = (
    ReportProperty("reportCode", "Mã báo cáo", "overview.report_code"),
    ReportProperty("reportType", "Loại báo cáo", "overview.report_type"),
    ReportProperty(
        "requesterUnitL1", "Đơn vị yêu cầu cấp 1", "overview.requester_unit_l1"
    ),
    ReportProperty(
        "requesterUnitL2", "Đơn vị yêu cầu cấp 2", "overview.requester_unit_l2"
    ),
    ReportProperty("businessContact", "Đầu mối nghiệp vụ", "overview.business_contact"),
    ReportProperty("dataUnit", "Đơn vị phụ trách (KDL)", "overview.data_unit"),
    ReportProperty("dataContact", "Đầu mối KDL", "overview.data_contact"),
    ReportProperty("usingUnit", "Đơn vị sử dụng", "overview.using_unit"),
    ReportProperty("usingTitle", "Chức danh sử dụng", "overview.using_title"),
    ReportProperty("dataPermission", "Phân quyền dữ liệu", "overview.data_permission"),
    ReportProperty("channel", "Kênh khai thác", "overview.channel"),
    ReportProperty("reportParams", "Tham số báo cáo", "overview.params"),
    ReportProperty("frequency", "Tần suất chạy báo cáo", "overview.frequency"),
    ReportProperty("dateRange", "Khoảng thời gian xuất dữ liệu", "overview.date_range"),
    ReportProperty(
        "historyRequired", "Yêu cầu dữ liệu lịch sử", "overview.history_required"
    ),
    ReportProperty(
        "dailyTime", "Thời gian có báo cáo trong ngày", "overview.daily_time"
    ),
    ReportProperty("sensitiveData", "Dữ liệu nhạy cảm", "overview.sensitive_data"),
    ReportProperty("reportFolder", "Thư mục đặt báo cáo", "overview.folder"),
    ReportProperty("sourceFile", "File yêu cầu nguồn", "source_file.name"),
    ReportProperty(
        "reportMockup",
        "Template / Mockup báo cáo (PL3.2)",
        "mockup_markdown",
        CustomPropertyDataTypes.MARKDOWN,
    ),
    ReportProperty(
        "impactAssessment",
        "Đánh giá tác động (PL3.5)",
        "impact_markdown",
        CustomPropertyDataTypes.MARKDOWN,
    ),
)


def _read_path(dashboard: ParsedDashboard, path: str) -> Optional[str]:
    value: Any = dashboard
    for part in path.split("."):
        value = getattr(value, part, None)
        if value is None:
            break
    return str(value) if value else None


def build_extension(dashboard: ParsedDashboard) -> Dict[str, Any]:
    """
    Values for the properties this workbook actually filled in.

    Empty fields are left out rather than stored as blanks, so the UI shows
    only what the report request declared.
    """
    extension = {}
    for spec in REPORT_PROPERTIES:
        value = _read_path(dashboard, spec.source)
        if value:
            extension[spec.name] = value
    return extension


def register_report_properties(metadata: OpenMetadata) -> bool:
    """
    Declare every report property on the `dashboard` Type.

    Returns whether the whole set is now registered. A single failure makes
    the caller fall back to the description, because a partially registered
    set would silently drop the fields that did not make it.
    """
    registered = False
    try:
        type_refs = {
            data_type: metadata.get_property_type_ref(data_type)
            for data_type in {spec.data_type for spec in REPORT_PROPERTIES}
        }
        for spec in REPORT_PROPERTIES:
            metadata.create_or_update_custom_property(
                OMetaCustomProperties(
                    entity_type=Dashboard,
                    createCustomPropertyRequest=CreateCustomPropertyRequest(
                        name=EntityName(spec.name),
                        displayName=spec.display_name,
                        description=Markdown(
                            f"{spec.display_name} - trích từ phiếu yêu cầu "
                            f"báo cáo (YCPT_BAOCAO)."
                        ),
                        propertyType=type_refs[spec.data_type],
                    ),
                )
            )
        registered = True
    except Exception as exc:
        logger.warning(
            f"Could not register report custom properties on the dashboard type "
            f"({exc}); falling back to the Markdown description. An "
            f"admin-capable ingestion bot is required to create them."
        )
        logger.debug("Custom property registration failed", exc_info=True)
    return registered
