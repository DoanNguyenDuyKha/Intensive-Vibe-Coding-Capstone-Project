import json
from unittest.mock import MagicMock, patch

from a2ui.basic_catalog import BasicCatalog
from a2ui.basic_catalog.constants import VERSION_0_9
from a2ui.schema.manager import A2uiSchemaManager
from a2ui.schema.validator import A2uiValidator

from app.agent import generate_sales_ui


def test_generate_sales_ui_validity_success():
    """TDD Green Phase Test: Mock the LLM to return a valid A2UI v0.9 Hybrid Output.

    Verifies that the workflow executes successfully and the generated JSON passes validation.
    """
    valid_hybrid_output = {
        "data": [
            {"region": "North", "quarter": "Q3", "revenue": 125000.0},
            {"region": "South", "quarter": "Q3", "revenue": 110000.0}
        ],
        "ui": {
            "version": "v0.9",
            "updateComponents": {
                "surfaceId": "sales-canvas",
                "components": [
                    {
                        "id": "root",
                        "component": "Column",
                        "children": ["title-card", "data-row"]
                    },
                    {
                        "id": "title-card",
                        "component": "Card",
                        "child": "title-text"
                    },
                    {
                        "id": "title-text",
                        "component": "Text",
                        "text": "Sales Dashboard Q3 & Q4"
                    },
                    {
                        "id": "data-row",
                        "component": "Row",
                        "children": ["north-card", "south-card"]
                    },
                    {
                        "id": "north-card",
                        "component": "Card",
                        "child": "north-text"
                    },
                    {
                        "id": "north-text",
                        "component": "Text",
                        "text": "North Region: $125,000"
                    },
                    {
                        "id": "south-card",
                        "component": "Card",
                        "child": "south-text"
                    },
                    {
                        "id": "south-text",
                        "component": "Text",
                        "text": "South Region: $110,000"
                    }
                ]
            }
        }
    }

    mock_response = MagicMock()
    mock_response.text = json.dumps(valid_hybrid_output)

    mock_sql_response = MagicMock()
    mock_sql_response.text = "SELECT region, quarter, revenue FROM sales;"

    # Patch the Client class directly to avoid real API calls in workflow nodes
    with patch("google.genai.Client") as mock_client_class:
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.models.generate_content.side_effect = [mock_sql_response, mock_response]

        ui_json = generate_sales_ui()
        assert mock_client.models.generate_content.call_count == 2

        # Validate the output using the v0.9 validator
        config = BasicCatalog.get_config(version=VERSION_0_9)
        manager = A2uiSchemaManager(version=VERSION_0_9, catalogs=[config])
        catalog = manager.get_selected_catalog()
        validator = A2uiValidator(catalog)

        # This should execute without raising any exception
        validator.validate(ui_json)


def test_generate_sales_ui_with_retry_recovery():
    """TDD Green Phase Test: Mock the LLM to return invalid JSON on the first attempt,

    and then a valid response on the second attempt. Verifies that the self-correction
    retry loop recovers and returns a valid layout.
    """
    valid_hybrid_output = {
        "data": [],
        "ui": {
            "version": "v0.9",
            "updateComponents": {
                "surfaceId": "sales-canvas",
                "components": [
                    {
                        "id": "root",
                        "component": "Column",
                        "children": ["title-text"]
                    },
                    {
                        "id": "title-text",
                        "component": "Text",
                        "text": "Sales Dashboard"
                    }
                ]
            }
        }
    }

    # First attempt: invalid version string (fails validation)
    invalid_hybrid_output = {
        "data": [],
        "ui": {
            "version": "v0.8",  # Invalid version, expected v0.9
            "updateComponents": {
                "surfaceId": "sales-canvas",
                "components": [
                    {
                        "id": "root",
                        "component": "Column",
                        "children": ["title-text"]
                    },
                    {
                        "id": "title-text",
                        "component": "Text",
                        "text": "Sales Dashboard"
                    }
                ]
            }
        }
    }

    mock_resp_1 = MagicMock()
    mock_resp_1.text = json.dumps(invalid_hybrid_output)

    mock_resp_2 = MagicMock()
    mock_resp_2.text = json.dumps(valid_hybrid_output)

    mock_sql_response = MagicMock()
    mock_sql_response.text = "SELECT region, quarter, revenue FROM sales;"

    # Patch the Client class directly to avoid real API calls in workflow nodes
    with patch("google.genai.Client") as mock_client_class:
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.models.generate_content.side_effect = [mock_sql_response, mock_resp_1, mock_resp_2]

        ui_json = generate_sales_ui()

        # Verify it was called three times (one for SQL, two for UI generation due to retry loop)
        assert mock_client.models.generate_content.call_count == 3

        # Validate the recovery layout
        config = BasicCatalog.get_config(version=VERSION_0_9)
        manager = A2uiSchemaManager(version=VERSION_0_9, catalogs=[config])
        catalog = manager.get_selected_catalog()
        validator = A2uiValidator(catalog)

        validator.validate(ui_json)
