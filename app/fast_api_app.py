# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os

# System proxy settings kept.
import google.auth
from fastapi import FastAPI
from fastapi.responses import FileResponse
from google.adk.cli.fast_api import get_fast_api_app
from google.cloud import logging as google_cloud_logging

from app.app_utils.telemetry import setup_telemetry
from app.app_utils.typing import Feedback

setup_telemetry()
_, project_id = google.auth.default()
logging_client = google_cloud_logging.Client()
logger = logging_client.logger(__name__)
allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

# Artifact bucket for ADK (created by Terraform, passed via env var)
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# In-memory session configuration - no persistent storage
session_service_uri = None

artifact_service_uri = f"gs://{logs_bucket_name}" if logs_bucket_name else None

app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=artifact_service_uri,
    allow_origins=allow_origins,
    session_service_uri=session_service_uri,
    otel_to_cloud=True,
)
app.title = "a2ui-data-canvas"
app.description = "API for interacting with the Agent a2ui-data-canvas"


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback.

    Args:
        feedback: The feedback data to log

    Returns:
        Success message
    """
    logger.log_struct(feedback.model_dump(), severity="INFO")
    return {"status": "success"}


# ---------------------------------------------------------------------------
# A2UI Data Canvas Dashboard Endpoints
# ---------------------------------------------------------------------------

from pathlib import Path

from fastapi import HTTPException
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel

from app.agent import sales_canvas_workflow


class PromptRequest(BaseModel):
    prompt: str


import sqlite3


class SalesRecordUpdate(BaseModel):
    revenue: float
    units_sold: int
    avg_deal_size: float


@app.get("/api/sales")
def get_sales_records():
    """Fetch all sales records from SQLite database."""
    db_path = Path(AGENT_DIR) / "data" / "sales.db"
    if not db_path.exists():
        db_path = Path(os.getcwd()) / "data" / "sales.db"

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sales")
        rows = cursor.fetchall()
        records = [dict(row) for row in rows]
        conn.close()
        return records
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e!s}")


@app.put("/api/sales/{record_id}")
def update_sales_record(record_id: int, record: SalesRecordUpdate):
    """Update a sales record in the database."""
    db_path = Path(AGENT_DIR) / "data" / "sales.db"
    if not db_path.exists():
        db_path = Path(os.getcwd()) / "data" / "sales.db"

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE sales
            SET revenue = ?, units_sold = ?, avg_deal_size = ?
            WHERE id = ?
            """,
            (record.revenue, record.units_sold, record.avg_deal_size, record_id)
        )
        conn.commit()
        updated = cursor.rowcount > 0
        conn.close()

        if not updated:
            raise HTTPException(status_code=404, detail="Record not found")
        return {"status": "success", "message": f"Record {record_id} updated."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e!s}")


@app.get("/canvas")
def read_canvas():
    """Serve the A2UI Data Canvas index.html file."""
    index_path = Path(AGENT_DIR) / "canvas_dashboard" / "index.html"
    if not index_path.exists():
        # Fallback for local dev running outside docker code root
        index_path = Path(AGENT_DIR) / "parent" / "canvas_dashboard" / "index.html"
        if not index_path.exists():
            index_path = Path(os.getcwd()) / "canvas_dashboard" / "index.html"
    return FileResponse(str(index_path))


@app.post("/api/generate")
def generate_canvas(request: PromptRequest):
    """Run the ADK 2.0 Graph Workflow and return the Hybrid Output JSON."""
    try:
        import uuid
        unique_id = str(uuid.uuid4())
        session_service = InMemorySessionService()
        session = session_service.create_session_sync(user_id=unique_id, app_name="canvas")
        runner = Runner(agent=sales_canvas_workflow, session_service=session_service, app_name="canvas")

        user_message = types.Content(
            role="user",
            parts=[types.Part.from_text(text=request.prompt)]
        )

        events = list(runner.run(
            user_id=unique_id,
            session_id=session.id,
            new_message=user_message
        ))

        # Compile structured trace events for frontend visualization
        trace_events = []
        for ev in events:
            ev_type = type(ev).__name__
            node_name = getattr(ev, "node_name", None)
            output_val = getattr(ev, "output", None)

            trace_events.append({
                "type": ev_type,
                "node_name": node_name,
                "output": str(output_val)[:1000] if output_val else None
            })

        final_output = session.state.get("final_output")
        raw_data = session.state.get("sales_data_raw")

        if not final_output:
            for ev in events:
                output_val = getattr(ev, "output", None)
                if output_val and isinstance(output_val, dict) and "ui" in output_val:
                    final_output = output_val
                    break

        if not final_output:
            raise ValueError("Workflow executed successfully but did not produce a valid Hybrid Output.")

        return {
            "data": final_output.get("data"),
            "ui": final_output.get("ui"),
            "trace": {
                "sql_data": raw_data,
                "events": trace_events
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

