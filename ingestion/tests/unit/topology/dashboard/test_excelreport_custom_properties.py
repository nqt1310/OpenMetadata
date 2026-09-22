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
Test that report-request fields land in Dashboard custom properties.

The workbook parsing is real (same synthetic fixture as the parser tests);
only the OpenMetadata server is mocked, since registering a custom property
is a server-side Type edit.
"""

import re
import shutil
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from metadata.generated.schema.type.customProperty import PropertyType
from metadata.generated.schema.type.entityReference import EntityReference
from metadata.ingestion.source.dashboard.excelreport import custom_properties, parser
from metadata.ingestion.source.dashboard.excelreport.metadata import ExcelReportSource

RESOURCE_PATH = (
    Path(__file__).parent.parent.parent / "resources/datasets/excelreport_sample.xlsx"
)

# Mirrors basic.json#/definitions/customPropertyName.
CUSTOM_PROPERTY_NAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9 _\-.,;%#@!'(){}\[\]|=+?`]*$"
)

SERVICE_NAME = "test_excel_reports"


@pytest.fixture
def workbook_dir(tmp_path):
    shutil.copy(RESOURCE_PATH, tmp_path / RESOURCE_PATH.name)
    return tmp_path


@pytest.fixture
def dashboard():
    return parser.parse_workbook(RESOURCE_PATH)[0]


def build_source(directory, **options):
    config = {
        "type": "customdashboard",
        "serviceName": SERVICE_NAME,
        "serviceConnection": {
            "config": {
                "type": "CustomDashboard",
                "sourcePythonClass": (
                    "metadata.ingestion.source.dashboard.excelreport.metadata."
                    "ExcelReportSource"
                ),
                "connectionOptions": {"directoryPath": str(directory), **options},
            }
        },
        "sourceConfig": {"config": {"type": "DashboardMetadata"}},
    }
    metadata = MagicMock()
    metadata.get_property_type_ref.return_value = PropertyType(
        EntityReference(id=uuid.uuid4(), type="type")
    )
    return ExcelReportSource.create(config, metadata)


class TestPropertyDefinitions:
    def test_every_property_name_is_accepted_by_the_schema(self):
        invalid = [
            spec.name
            for spec in custom_properties.REPORT_PROPERTIES
            if not CUSTOM_PROPERTY_NAME_RE.match(spec.name)
        ]

        assert invalid == []

    def test_property_names_are_unique(self):
        names = [spec.name for spec in custom_properties.REPORT_PROPERTIES]

        assert len(names) == len(set(names))

    def test_every_source_path_resolves_on_the_parsed_model(self, dashboard):
        # A typo in a dotted path would silently drop that field forever.
        unresolvable = []
        for spec in custom_properties.REPORT_PROPERTIES:
            target = dashboard
            for part in spec.source.split("."):
                if not hasattr(target, part):
                    unresolvable.append(spec.source)
                    break
                target = getattr(target, part)

        assert unresolvable == []


class TestBuildExtension:
    def test_workbook_fields_become_property_values(self, dashboard):
        extension = custom_properties.build_extension(dashboard)

        assert extension["reportCode"] == "TEST_M01"
        assert extension["frequency"] == "Tháng"
        assert extension["sensitiveData"] == "1.Có"
        assert extension["businessContact"] == "owner.test@example.com"

    def test_source_file_is_recorded_by_name(self, dashboard):
        extension = custom_properties.build_extension(dashboard)

        assert extension["sourceFile"] == "excelreport_sample.xlsx"

    def test_long_form_sheets_become_markdown_properties(self, dashboard):
        extension = custom_properties.build_extension(dashboard)

        assert "Cột A" in extension["reportMockup"]
        assert "Hệ thống báo cáo nội bộ" in extension["impactAssessment"]

    def test_blank_fields_are_left_out_rather_than_stored_empty(self, dashboard):
        dashboard.overview.channel = None
        dashboard.overview.params = ""

        extension = custom_properties.build_extension(dashboard)

        assert "channel" not in extension
        assert "reportParams" not in extension


class TestRegistration:
    def test_one_call_per_declared_property(self, workbook_dir):
        source = build_source(workbook_dir)

        source.prepare()

        assert source.store_extension is True
        assert source.metadata.create_or_update_custom_property.call_count == len(
            custom_properties.REPORT_PROPERTIES
        )

    def test_a_restricted_bot_degrades_instead_of_failing(self, workbook_dir):
        source = build_source(workbook_dir)
        source.metadata.create_or_update_custom_property.side_effect = RuntimeError(
            "403 Forbidden"
        )

        source.prepare()

        assert source.store_extension is False

    def test_registration_can_be_switched_off(self, workbook_dir):
        source = build_source(workbook_dir, registerCustomProperties="false")

        source.prepare()

        assert source.store_extension is False
        source.metadata.create_or_update_custom_property.assert_not_called()


class TestDescriptionSplit:
    def test_description_holds_only_the_purpose_once_properties_exist(
        self, workbook_dir, dashboard
    ):
        source = build_source(workbook_dir)
        source.prepare()

        description = source._build_description(dashboard)

        assert description is not None
        assert "Thông tin báo cáo" not in description.root
        assert "| Tần suất chạy báo cáo |" not in description.root

    def test_full_table_is_kept_when_properties_are_unavailable(
        self, workbook_dir, dashboard
    ):
        source = build_source(workbook_dir)
        source.store_extension = False

        description = source._build_description(dashboard)

        assert "Thông tin báo cáo" in description.root
        assert "Tần suất chạy báo cáo" in description.root

    def test_extension_is_only_populated_when_registered(self, workbook_dir, dashboard):
        source = build_source(workbook_dir)

        source.store_extension = False
        assert source._build_extension(dashboard) is None

        source.store_extension = True
        assert source._build_extension(dashboard)["reportCode"] == "TEST_M01"
