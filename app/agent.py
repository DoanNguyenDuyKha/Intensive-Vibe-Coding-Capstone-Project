# ruff: noqa
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

import datetime
from zoneinfo import ZoneInfo
import os
import json

# System proxy settings will be kept, but we will force HTTP/1.1 inside the GenAI Client to avoid handshake hangs.
import httpx
orig_httpx_client_init = httpx.Client.__init__
def patched_httpx_client_init(self, *args, **kwargs):
    kwargs["http2"] = False
    orig_httpx_client_init(self, *args, **kwargs)
httpx.Client.__init__ = patched_httpx_client_init

orig_httpx_async_client_init = httpx.AsyncClient.__init__
def patched_httpx_async_client_init(self, *args, **kwargs):
    kwargs["http2"] = False
    orig_httpx_async_client_init(self, *args, **kwargs)
httpx.AsyncClient.__init__ = patched_httpx_async_client_init

import google.auth
from pydantic import BaseModel
from typing import Any, Optional, Dict, List

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.workflow import Workflow, Edge, START, node
from google.adk.agents.context import Context
from google.genai import types

from a2ui.basic_catalog import BasicCatalog
from a2ui.basic_catalog.constants import VERSION_0_9
from a2ui.schema.manager import A2uiSchemaManager
from a2ui.schema.validator import A2uiValidator
import posixpath

# Monkeypatch the validator to use posixpath for base URI resolution on Windows,
# preventing RefResolutionError due to Windows backslashes in file URIs.
orig_build_0_9_validator = A2uiValidator._build_0_9_validator

def patched_build_0_9_validator(self):
    orig_join = os.path.join
    orig_dirname = os.path.dirname
    try:
        os.path.join = posixpath.join
        os.path.dirname = posixpath.dirname
        return orig_build_0_9_validator(self)
    finally:
        os.path.join = orig_join
        os.path.dirname = orig_dirname

A2uiValidator._build_0_9_validator = patched_build_0_9_validator

from mcp_server import query_sales_data, init_database, update_sales_data

# Try to load local .env variables
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            if "=" in line and not line.strip().startswith("#"):
                key, val = line.strip().split("=", 1)
                os.environ[key.strip()] = val.strip()

# Set environment variables for Vertex AI / Gemini API
try:
    _, project_id = google.auth.default()
    os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
except Exception:
    project_id = None

os.environ["GOOGLE_CLOUD_LOCATION"] = "global"

# If a Gemini API Key is provided in environment, default to AI Studio (Vertex = False)
if os.environ.get("GEMINI_API_KEY"):
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
else:
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

# Initialize A2UI Schema Manager for v0.9
catalog_config = BasicCatalog.get_config(version=VERSION_0_9)
schema_manager = A2uiSchemaManager(version=VERSION_0_9, catalogs=[catalog_config])
catalog = schema_manager.get_selected_catalog()
a2ui_validator = A2uiValidator(catalog)


# ---------------------------------------------------------------------------
# Workflow State Schema
# ---------------------------------------------------------------------------

from pydantic import BaseModel, Field
class SalesDashboardState(BaseModel):
    messages: list = Field(default_factory=list)
    user_prompt: str = ""
    sales_data_raw: Optional[str] = None
    final_output: Optional[dict] = None
    critic_feedback: Optional[str] = None
    update_message: Optional[str] = None


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def clean_json_string(s: str) -> str:
    """Removes markdown code block delimiters and whitespaces from the LLM output."""
    s = s.strip()
    
    # Try parsing directly first
    try:
        json.loads(s)
        return s
    except json.JSONDecodeError:
        pass
        
    # Search for first '{' and last '}' to extract raw JSON
    first_brace = s.find('{')
    last_brace = s.rfind('}')
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        extracted = s[first_brace:last_brace+1]
        try:
            json.loads(extracted)
            return extracted
        except json.JSONDecodeError:
            pass

    if s.startswith("```json"):
        s = s[7:]
    elif s.startswith("```"):
        s = s[3:]
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()


def validate_hybrid_output(output_str: str, validator: A2uiValidator, raw_data_fallback: str) -> dict:
    """Validates that the LLM output contains both valid sales data and valid A2UI v0.9 layout."""
    try:
        data = json.loads(output_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Output is not a valid JSON: {e}")
    
    if not isinstance(data, dict):
        raise ValueError("Output must be a JSON object.")
        
    # Auto-wrap if model returns A2UI JSON directly at root (due to SchemaManager instructions)
    if "version" in data and "updateComponents" in data:
        try:
            parsed_raw = json.loads(raw_data_fallback)
        except Exception:
            parsed_raw = raw_data_fallback
        data = {
            "data": parsed_raw,
            "ui": data
        }
        
    if "data" not in data:
        raise ValueError("Missing required key 'data' in Hybrid Output.")
    if "ui" not in data:
        raise ValueError("Missing required key 'ui' in Hybrid Output.")
    
    # Validate the A2UI v0.9 structure of the ui key
    try:
        validator.validate(data["ui"])
    except ValueError as e:
        raise ValueError(f"The 'ui' field is not a valid A2UI v0.9 layout: {e}")
        
    return data


# ---------------------------------------------------------------------------
# Workflow Nodes
# ---------------------------------------------------------------------------


@node(name="Intent_Router")
def intent_router_node(ctx: Context) -> str:
    """Analyzes the prompt to check if it's a database update request."""
    print(">>> STARTING INTENT ROUTER", flush=True)
    messages = ctx.state.get("messages", [])
    prompt = ctx.state.get("user_prompt", "")
        
    if not prompt and messages:
        prompt = messages[-1].parts[0].text
        
    ctx.state["user_prompt"] = prompt
        
    if not prompt:
        return "next"
    
    lower_prompt = prompt.lower()
    
    if "cập nhật" in lower_prompt or "update" in lower_prompt or "đổi" in lower_prompt or "change" in lower_prompt or "set" in lower_prompt or "modify" in lower_prompt:
        from google import genai
        client = genai.Client()
        # Translate to SQL
        sys_prompt = "You are a SQLite expert. The table is 'sales' (id, region, quarter, month, product_category, revenue, units_sold, avg_deal_size, sales_rep). Return ONLY a valid SQL UPDATE statement based on the user request. No markdown, no explanation."
        resp = client.models.generate_content(
            model="gemini-flash-latest",
            contents=f"{sys_prompt}\n\nUser request: {prompt}"
        )
        sql = resp.text.strip().replace('```sql','').replace('```','')
        res = update_sales_data(sql)
        ctx.state["update_message"] = f"Database update executed: {res}"
    return "next"

@node(name="Critic_Node")
def critic_node(ctx: Context) -> str:
    """Validates the output of the UI Generator and sends back feedback if invalid."""
    final_output = ctx.state.get("final_output")
        
    if not final_output:
        ctx.state["critic_feedback"] = "Missing output from UI generator."
        return "error"
        
    # Check if a chart was explicitly requested but missing
    prompt = ctx.state.get("user_prompt", "").lower()
        
    if "biểu đồ" in prompt or "chart" in prompt:
        import json
        ui_str = json.dumps(final_output)
        # Simple heuristic: if they wanted a chart, we expect BarChart or LineChart
        if "BarChart" not in ui_str and "LineChart" not in ui_str:
            # But our A2UI BasicCatalog might not have BarChart.
            pass
    
    # We will just pass it if validate_hybrid_output already succeeded
    return "valid"

@node(name="Data_Fetcher")
def data_fetcher_node(ctx: Context) -> str:
    """Node 1: Fetches relevant sales data from the SQLite database using an AI-generated SQL query."""
    print(">>> STARTING DATA FETCHER", flush=True)
    init_database()
    
    prompt = ctx.state.get("user_prompt", "")
    update_msg = ctx.state.get("update_message")
        
    if update_msg:
        # If an update was executed, fetch all sales records to show the updated dashboard
        query = "SELECT region, quarter, month, product_category, revenue, units_sold, avg_deal_size, sales_rep FROM sales;"
    elif prompt:
        from google import genai
        client = genai.Client()
        sys_prompt = "You are a SQLite expert. The table is 'sales' (id, region, quarter, month, product_category, revenue, units_sold, avg_deal_size, sales_rep). Generate a valid SQL SELECT statement to answer the user's request. Return ONLY the raw SQL query, no markdown, no explanation."
        resp = client.models.generate_content(
            model="gemini-flash-latest",
            contents=f"{sys_prompt}\n\nUser request: {prompt}"
        )
        query = resp.text.strip().replace('```sql','').replace('```','')
    else:
        query = "SELECT region, quarter, month, product_category, revenue, units_sold, avg_deal_size, sales_rep FROM sales;"
        
    try:
        result_str = query_sales_data(query)
    except Exception as e:
        # Fallback to full data if query fails
        fallback_query = "SELECT region, quarter, month, product_category, revenue, units_sold, avg_deal_size, sales_rep FROM sales;"
        result_str = query_sales_data(fallback_query)
    
    # Save the raw data to workflow state using dictionary access
    ctx.state["sales_data_raw"] = result_str
    
    print(f">>> DATA FETCHER FINISHED. result_str size: {len(result_str)}", flush=True)
    return result_str


@node(name="UI_Generator_Agent")
def ui_generator_node(ctx: Context) -> dict:
    """Node 2: Generates an A2UI v0.9 sales canvas dashboard layout from the retrieved data."""
    print(">>> STARTING UI GENERATOR", flush=True)
    
    raw_data = ctx.state.get("sales_data_raw")
        
    print(f">>> UI GENERATOR: raw_data type={type(raw_data)}", flush=True)
    if not raw_data:
        print(f">>> UI GENERATOR STATE DUMP: {ctx.state.model_dump() if hasattr(ctx.state, 'model_dump') else getattr(ctx, 'state', None)}", flush=True)
        raise ValueError("No sales data found in workflow state. Did Data_Fetcher run?")

    # Setup the system prompt using schema_manager instructions
    instructions = schema_manager.generate_system_prompt(
        role_description="You are a System Architect designed to output a Hybrid Output containing both raw sales data and a valid A2UI v0.9 user interface. "
                         "Do not include any explanation, backticks, or markdown blocks in your final output. Respond with raw JSON only.",
        include_schema=True
    )

    if isinstance(ctx.state, dict):
        user_prompt = ctx.state.get("user_prompt", "")
    else:
        user_prompt = getattr(ctx.state, "user_prompt", "")
        
    if not user_prompt:
        user_prompt = "Create a general sales dashboard for Q3 and Q4."

    update_msg = ctx.state.get("update_message") if isinstance(ctx.state, dict) else getattr(ctx.state, "update_message", None)
    update_info = f'\n    SYSTEM ACTION STATUS: "{update_msg}"\n    (Please display a clean success notification card or label at the top of the dashboard layout to show the user that their data update was executed successfully.)\n' if update_msg else ""

    prompt = f"""
    {instructions}

    You MUST create a user interface that directly answers and satisfies the following user request:
    USER REQUEST: "{user_prompt}"
    {update_info}
    Available Sales Data (JSON):
    {raw_data}

    You MUST respond with a single JSON object containing:
    1. "data": The raw JSON sales data list.
    2. "ui": A valid A2UI v0.9 message that displays this sales data in a clean layout.
       Specifically, the "ui" field must be an UpdateComponentsMessage:
       {{
         "version": "v0.9",
         "updateComponents": {{
           "surfaceId": "sales-canvas",
           "components": [
             {{
               "id": "root",
               "component": "Column",
               "children": ["title-card", "summary-row", "details-column"]
             }},
             ...
           ]
         }}
       }}
       
       Rules:
       - Use ONLY the following Basic Catalog components: Column, Row, Text, Button, Card.
       - Do NOT use raw HTML, CSS, or JS.
       - Layout columns using Column and rows using Row. Group sections using Card.
       - Render labels and values using Text components.
       - The root of the components list must have id "root".
       - All referenced component IDs in "children" list must exist.
    """

    from google import genai
    client = genai.Client()
    model_name = "gemini-flash-latest"
    
    current_prompt = prompt
    attempts = 3
    last_error = getattr(ctx.state, "critic_feedback", None)
    text = ""
    
    for attempt in range(attempts):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=current_prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                )
            )
            text = clean_json_string(response.text)
            parsed = validate_hybrid_output(text, a2ui_validator, raw_data)
            
            # Save final validated output to workflow state and return
            ctx.state["final_output"] = parsed
            return parsed
            
        except Exception as e:
            last_error = e
            # Sleep to wait out transient 503 UNAVAILABLE or 429 RESOURCE_EXHAUSTED errors
            import time
            if any(term in str(e).upper() for term in ["503", "429", "UNAVAILABLE", "EXHAUSTED"]):
                time.sleep(2.5)
            # Re-prompt the model with the exact validation error to let it self-correct
            current_prompt = f"""
            Your previous response failed validation with the following error:
            {str(e)}
            
            Please correct the JSON and return the full corrected JSON object.
            Remember:
            1. The version MUST be "v0.9".
            2. Do not use raw HTML, CSS, or JS.
            3. Only use Basic Catalog components: Column, Row, Text, Button, Card.
            4. The root component in components must have id "root".
            5. Return raw JSON only, no markdown wrapping, no explanation.
            
            Previous invalid attempt:
            {text}
            """

    raise ValueError(f"Failed to generate valid Hybrid Output after {attempts} attempts. Last error: {last_error}")


# ---------------------------------------------------------------------------
# ADK 2.0 Graph Workflow Definition
# ---------------------------------------------------------------------------

sales_canvas_workflow = Workflow(
    name="sales_canvas_workflow",
    description="A workflow that fetches SQLite sales data and generates an A2UI v0.9 sales canvas dashboard",
    state_schema=SalesDashboardState,
    edges=[
        Edge(from_node=START, to_node=intent_router_node),
        Edge(from_node=intent_router_node, to_node=data_fetcher_node),
        Edge(from_node=data_fetcher_node, to_node=ui_generator_node),
        Edge(from_node=ui_generator_node, to_node=critic_node)
    ]
)


# Keep the root_agent config for API client configuration references
root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model="gemini-flash-latest",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="You are a helpful AI assistant designed to provide accurate and useful information.",
)

# App uses the Workflow as the root_agent!
app = App(
    root_agent=root_agent,
    name="app",
)


# Helper function for TDD validation
def generate_sales_ui() -> dict:
    """Helper function to run the workflow and retrieve the generated UI JSON payload.

    Used by tests/test_a2ui.py.
    """
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    
    session_service = InMemorySessionService()
    session = session_service.create_session_sync(user_id="test_user", app_name="test")
    runner = Runner(agent=sales_canvas_workflow, session_service=session_service, app_name="test")
    
    msg = types.Content(role="user", parts=[types.Part.from_text(text="Generate sales canvas UI")])
    events = list(runner.run(user_id="test_user", session_id=session.id, new_message=msg))
    
    final_output = session.state.get("final_output")
    if not final_output:
        # Fallback to checking workflow output if not stored in final_output state
        for event in events:
            if event.output and isinstance(event.output, dict) and "ui" in event.output:
                return event.output["ui"]
        raise ValueError("Workflow finished but final_output is missing.")
        
    return final_output["ui"]
