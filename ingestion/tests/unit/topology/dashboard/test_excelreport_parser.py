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
Test the Excel Report (QT.B14.QLDL.MB06.1 YCPT_BAOCAO) workbook parser.

The fixture is a sanitized synthetic workbook that mirrors the real
template's sheet/header layout (report overview + mockup + mapping +
impact-assessment sheets, plus helper/legend rows above the real
header), without any real report data.
"""

from pathlib import Path

from metadata.ingestion.source.dashboard.excelreport import parser

RESOURCE_PATH = Path(__file__).parent.parent.parent / "resources/datasets/excelreport_sample.xlsx"


def test_parse_workbook_single_dashboard():
    dashboards = parser.parse_workbook(RESOURCE_PATH)

    assert len(dashboards) == 1
    dashboard = dashboards[0]
    assert dashboard.dashboard_key == "TEST_M01"
    assert dashboard.overview.report_name == "TEST_Báo cáo mẫu top 10"
    assert dashboard.overview.link == "https://reports.example.internal/test"
    assert dashboard.overview.business_contact == "owner.test@example.com"
    assert dashboard.overview.frequency == "Tháng"
    assert dashboard.overview.sensitive_data == "1.Có"


def test_parse_workbook_keeps_mockup_and_impact_as_reference_text():
    dashboard = parser.parse_workbook(RESOURCE_PATH)[0]

    assert dashboard.mockup_markdown is not None
    assert "Cột A" in dashboard.mockup_markdown
    assert dashboard.impact_markdown is not None
    assert "Hệ thống báo cáo nội bộ" in dashboard.impact_markdown


def test_mapping_rows_are_grouped_by_sheet_name():
    dashboard = parser.parse_workbook(RESOURCE_PATH)[0]

    # Sheet1 groups two rows that have different "Tên mục/biểu đồ" values
    # (Filter/Bộ lọc vs Bảng chi tiết) - sheet_name wins over section_name.
    assert dashboard.chart_groups() == ["Sheet1", "Sheet2"]

    sheet1_rows = dashboard.rows_for_chart_group("Sheet1")
    assert len(sheet1_rows) == 2
    assert {row.section_name for row in sheet1_rows} == {"Filter/Bộ lọc", "Bảng chi tiết"}
    assert sheet1_rows[0].source_schema == "rpt_test"
    assert sheet1_rows[0].source_table == "RPT_TEST_TOP10"
    assert sheet1_rows[0].source_field == "TEST_CUST_ID"
    assert sheet1_rows[0].has_source_reference is True

    sheet2_rows = dashboard.rows_for_chart_group("Sheet2")
    assert len(sheet2_rows) == 1
    assert sheet2_rows[0].indicator_name == "Ghi chú thủ công"
    assert sheet2_rows[0].has_source_reference is False


def test_slugify_is_ascii_and_stable():
    assert parser.slugify("Filter/Bộ lọc") == "filter_bo_loc"
    assert parser.slugify("Bảng chi tiết") == "bang_chi_tiet"


def test_render_dashboard_description_includes_key_sections():
    dashboard = parser.parse_workbook(RESOURCE_PATH)[0]
    description = parser.render_dashboard_description(dashboard)

    assert "## Thông tin báo cáo" in description
    assert "## Template / Mockup báo cáo (PL3.2)" in description
    assert "## Đánh giá tác động (PL3.5)" in description


def test_render_chart_description_lists_source_mapping():
    dashboard = parser.parse_workbook(RESOURCE_PATH)[0]
    description = parser.render_chart_description(dashboard.rows_for_chart_group("Sheet1"))

    assert "rpt_test.RPT_TEST_TOP10.TEST_CUST_ID" in description
    assert "Filter/Bộ lọc" in description
    assert "Bảng chi tiết" in description
