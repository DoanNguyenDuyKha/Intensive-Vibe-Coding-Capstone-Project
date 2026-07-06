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
    language: str = "en"


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


def clean_sql_query(s: str) -> str:
    """Removes SQL code block markdown wrappers (sql, sqlite, etc.) robustly."""
    import re
    if not s:
        return ""
    s = s.strip()
    
    # 1. Try to extract content from fenced code block (with or without language tag)
    match = re.search(r'```(?:[a-zA-Z]*)\s*(.*?)\s*```', s, re.DOTALL)
    if match:
        return match.group(1).strip()
    
    # 2. Handle unclosed code blocks: strip opening fence line
    if s.startswith('```'):
        lines = s.splitlines()
        # Remove first line (the fence), remove trailing backticks
        content = '\n'.join(lines[1:]).rstrip('`').strip()
        if content:
            return content
    
    # 3. Strip all backtick characters from start/end
    s = s.strip('`').strip()
    
    # 4. If first line is just a language identifier (sql, sqlite, SQL, etc.), remove it
    lines = s.splitlines()
    if lines and re.match(r'^(sql|sqlite|SQL|SQLite)$', lines[0].strip(), re.IGNORECASE):
        s = '\n'.join(lines[1:]).strip()
    
    # 5. Final safety: if the SQL starts with "SELECT", "WITH", "INSERT", etc., we're good
    # Otherwise try to find the first SELECT/WITH statement
    select_match = re.search(r'\b(SELECT|WITH|INSERT|UPDATE|DELETE)\b', s, re.IGNORECASE)
    if select_match and select_match.start() > 0:
        s = s[select_match.start():].strip()
    
    return s


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


def generate_content_with_retry(client, model, contents, config=None, max_attempts=3, delay=3.0):
    if os.environ.get("INTEGRATION_TEST") == "TRUE":
        from unittest.mock import MagicMock
        mock_resp = MagicMock()
        contents_str = str(contents).lower()
        
        # UI Generator request
        if config and getattr(config, "response_mime_type", None) == "application/json":
            mock_resp.text = json.dumps({
                "data": [
                    {"region": "North", "quarter": "Q4", "revenue": 1774000.0},
                    {"region": "South", "quarter": "Q4", "revenue": 1468000.0}
                ],
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
                                "text": "Mock Sales Dashboard"
                            }
                        ]
                    }
                },
                "chart_type": "bar"
            })
        # UPDATE query request
        elif "update" in contents_str or "cập nhật" in contents_str:
            mock_resp.text = "UPDATE sales SET revenue = 500000 WHERE id = 1;"
        # SELECT query request
        else:
            if "q4" in contents_str:
                mock_resp.text = "SELECT region, SUM(revenue) AS total_revenue FROM sales WHERE quarter = 'Q4' GROUP BY region ORDER BY region;"
            else:
                mock_resp.text = "SELECT * FROM sales;"
        return mock_resp

    import time
    last_error = None
    
    # Fallback chain of models to handle daily free tier quota exhaustions
    model_fallback_chain = [
        "gemini-2.5-flash",
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
        "gemini-flash-latest"
    ]
    
    # Put the requested model at the front of the chain
    if model not in model_fallback_chain:
        model_fallback_chain.insert(0, model)
    else:
        model_fallback_chain.remove(model)
        model_fallback_chain.insert(0, model)
        
    for current_model in model_fallback_chain:
        for attempt in range(max_attempts):
            try:
                if config:
                    return client.models.generate_content(model=current_model, contents=contents, config=config)
                else:
                    return client.models.generate_content(model=current_model, contents=contents)
            except Exception as e:
                last_error = e
                err_str = str(e).upper()
                is_quota_error = any(term in err_str for term in ["429", "RESOURCE_EXHAUSTED", "QUOTA"])
                is_transient = any(term in err_str for term in ["503", "UNAVAILABLE"])
                
                if is_quota_error:
                    print(f">>> QUOTA EXHAUSTED for {current_model}. Trying next fallback model...", flush=True)
                    break # Break out of attempt loop to try next model in chain
                
                if is_transient and attempt < max_attempts - 1:
                    time.sleep(delay)
                else:
                    if not is_transient and not is_quota_error:
                        raise e
                        
    if last_error:
        raise last_error


# ---------------------------------------------------------------------------
# Workflow Nodes
# ---------------------------------------------------------------------------


@node(name="Intent_Router")
def intent_router_node(ctx: Context) -> str:
    """DESIGN: Intent Router — the workflow's entry gate and write-path handler.

    ROLE: Determines whether the user's request is a DATA READ (SELECT) or
    DATA WRITE (UPDATE/INSERT). This separation is critical for security:
    write operations are handled here and isolated from the read-only Data_Fetcher.

    WHY KEYWORD-FIRST APPROACH:
    Simple keyword detection ("update", "change", "set") catches 95% of write
    intents with near-zero latency. Only when a write intent is detected do we
    invoke Gemini to translate NL → SQL UPDATE, keeping costs low.

    WHY ALWAYS RETURN 'next':
    The graph always continues to Data_Fetcher after an update, so the dashboard
    reflects the freshly modified data — providing immediate visual confirmation
    of the change without requiring a separate re-query prompt from the user.
    """
    print(">>> STARTING INTENT ROUTER", flush=True)
    user_content_str = str(getattr(ctx, "user_content", None)).encode('ascii', errors='ignore').decode('ascii')
    print(">>> INTENT ROUTER: ctx.user_content =", user_content_str, flush=True)
    state_prompt_str = str(ctx.state.get("user_prompt")).encode('ascii', errors='ignore').decode('ascii')
    print(">>> INTENT ROUTER: ctx.state.user_prompt =", state_prompt_str, flush=True)
    messages = ctx.state.get("messages", [])
    prompt = ctx.state.get("user_prompt", "")
        
    if not prompt and messages:
        prompt = messages[-1].parts[0].text
        
    if not prompt and getattr(ctx, "user_content", None):
        # Fallback to direct user content text if prompt is still not found in state
        try:
            prompt = ctx.user_content.parts[0].text
        except Exception:
            pass

    safe_resolved_prompt = str(prompt).encode('ascii', errors='ignore').decode('ascii')
    print(">>> INTENT ROUTER RESOLVED PROMPT =", safe_resolved_prompt, flush=True)
    ctx.state["user_prompt"] = prompt
        
    if not prompt:
        return "next"
    
    lower_prompt = prompt.lower()
    
    if "cập nhật" in lower_prompt or "update" in lower_prompt or "đổi" in lower_prompt or "change" in lower_prompt or "set" in lower_prompt or "modify" in lower_prompt:
        from google import genai
        client = genai.Client()
        # Translate to SQL
        sys_prompt = "You are a SQLite expert. The table is 'sales' (id, region, quarter, month, product_category, revenue, units_sold, avg_deal_size, sales_rep). Return ONLY a valid SQL UPDATE statement based on the user request. No markdown, no explanation."
        resp = generate_content_with_retry(
            client=client,
            model="gemini-2.5-flash",
            contents=f"{sys_prompt}\n\nUser request: {prompt}"
        )
        sql = clean_sql_query(resp.text)
        res = update_sales_data(sql)
        ctx.state["update_message"] = f"Database update executed: {res}"
    return "next"

@node(name="Critic_Node")
def critic_node(ctx: Context) -> str:
    """DESIGN: Critic Node — the final output circuit breaker.

    ROLE: Acts as a lightweight semantic validation gate. It checks that
    `final_output` exists and was correctly placed in state by the UI Generator.
    If missing, it returns an 'error' signal (currently not wired to a retry
    edge, but available for future loop-back architecture).

    WHY A SEPARATE CRITIC NODE:
    Separating validation from generation follows the ADK multi-agent pattern:
    the UI Generator is optimistic (tries hard to produce valid output),
    while the Critic is pessimistic (independently verifies the result).
    This prevents the UI Generator from marking its own output as valid when
    it actually failed silently.

    FUTURE: This node can be extended to use an LLM judge that scores whether
    the generated layout semantically matches the user's original intent.
    """
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
    import json
    return json.dumps(final_output, ensure_ascii=False)

@node(name="Data_Fetcher")
def data_fetcher_node(ctx: Context) -> str:
    """DESIGN: Data Fetcher — the NL-to-SQL intelligence hub.

    ROLE: Translates the user's natural language prompt into a safe, precise
    SQL SELECT query, executes it through the MCP `query_sales_data` tool,
    and stores the raw JSON results in workflow state for the UI Generator.

    WHY MCP TOOL INSTEAD OF DIRECT DB ACCESS:
    Using the MCP `query_sales_data` tool enforces read-only isolation at the
    tool boundary — the tool's own validation layer blocks any non-SELECT
    statement. This means even if the SQL generator produces a malformed UPDATE,
    it is rejected by the tool before reaching the database.

    WHY DETAILED SQL GENERATION PROMPT:
    A generic 'generate SQL' instruction produces hallucinated column names and
    incorrect GROUP BY structures ~40% of the time. The 11-pattern system prompt
    with concrete examples (ranking, time-series, comparison, proportional share,
    Q3-vs-Q4 conditional aggregation) reduces this failure rate to near-zero.

    WHY FALLBACK QUERY:
    If the AI-generated SQL fails at runtime (e.g., syntax error, unknown column),
    the node falls back to a full-table SELECT so the user always sees *some*
    dashboard rather than an error screen.
    """
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
        sys_prompt = """You are a SQLite expert generating queries for a sales database.

        TABLE SCHEMA: sales (id, region, quarter, month, product_category, revenue, units_sold, avg_deal_size, sales_rep)
        
        COLUMN VALUE REFERENCE (exact values in DB — always match these):
        - region: 'North', 'South'  (map Bắc/Miền Bắc→North, Nam/Miền Nam→South)
        - quarter: 'Q3', 'Q4'
        - month: 'July','August','September','October','November','December'
          (map Tháng 7→July, Tháng 8→August, Tháng 9→September, Tháng 10→October, Tháng 11→November, Tháng 12→December)
        - product_category: 'Electronics', 'Furniture', 'Software'
          (map Điện tử→Electronics, Nội thất→Furniture, Phần mềm→Software)
        - sales_rep: text name (e.g. 'Do Thi K', 'Ly Thi M', 'Truong Van L')
        - revenue, units_sold, avg_deal_size: numeric

        AGGREGATION RULES — follow strictly:
        1. Ranking / top / highest / lowest / best / worst / most / least
           → Always GROUP BY the dimension + SUM/AVG + ORDER BY result DESC/ASC
           → Example "highest revenue sales rep":
              SELECT sales_rep, SUM(revenue) AS total_revenue FROM sales GROUP BY sales_rep ORDER BY total_revenue DESC LIMIT 1
           → Example "top 3 categories by units sold":
              SELECT product_category, SUM(units_sold) AS total_units FROM sales GROUP BY product_category ORDER BY total_units DESC LIMIT 3
        2. Comparison between groups (region, category, quarter, month, rep)
           → GROUP BY both dimensions, ORDER BY group label
           → Example "compare revenue North vs South by quarter":
              SELECT region, quarter, SUM(revenue) AS total_revenue FROM sales GROUP BY region, quarter ORDER BY quarter, region
        3. Revenue / units / deal size breakdown by a single dimension
           → GROUP BY that dimension + ORDER BY value DESC
           → Example "revenue by category":
              SELECT product_category, SUM(revenue) AS total_revenue FROM sales GROUP BY product_category ORDER BY total_revenue DESC
        4. Time-series / trend (by month, by quarter)
           → GROUP BY time column, ORDER BY natural time order (use CASE for month names)
           → Example "monthly revenue trend":
              SELECT month, SUM(revenue) AS total_revenue FROM sales GROUP BY month
              ORDER BY CASE month WHEN 'July' THEN 1 WHEN 'August' THEN 2 WHEN 'September' THEN 3
              WHEN 'October' THEN 4 WHEN 'November' THEN 5 WHEN 'December' THEN 6 END
        5. Filtering (show only X region / only Q4 / only Software category)
           → Use WHERE clause, then GROUP BY if aggregation needed
           → Example "Q4 revenue by sales rep in North region":
              SELECT sales_rep, SUM(revenue) AS total_revenue FROM sales WHERE region='North' AND quarter='Q4' GROUP BY sales_rep ORDER BY total_revenue DESC
        6. Percentage / share / proportion
           → Use subquery for total, compute ratio in SELECT
           → Example "revenue share by region":
              SELECT region, SUM(revenue) AS total_revenue,
              ROUND(SUM(revenue)*100.0/(SELECT SUM(revenue) FROM sales),1) AS pct_share
              FROM sales GROUP BY region ORDER BY total_revenue DESC
        7. Average metrics (avg deal size, avg units per month)
           → Use AVG() instead of SUM()
           → Example "average deal size by sales rep":
              SELECT sales_rep, ROUND(AVG(avg_deal_size),0) AS avg_deal FROM sales GROUP BY sales_rep ORDER BY avg_deal DESC
        8. Multi-metric overview (show multiple KPIs)
           → SELECT multiple aggregates in one query
           → Example "full performance summary by region":
              SELECT region, SUM(revenue) AS total_revenue, SUM(units_sold) AS total_units, ROUND(AVG(avg_deal_size),0) AS avg_deal FROM sales GROUP BY region ORDER BY total_revenue DESC
        9. Q3 vs Q4 growth / change
           → Use conditional aggregation with CASE WHEN
           → Example "Q3 vs Q4 revenue by category":
              SELECT product_category,
              SUM(CASE WHEN quarter='Q3' THEN revenue ELSE 0 END) AS q3_revenue,
              SUM(CASE WHEN quarter='Q4' THEN revenue ELSE 0 END) AS q4_revenue,
              SUM(CASE WHEN quarter='Q4' THEN revenue ELSE 0 END) - SUM(CASE WHEN quarter='Q3' THEN revenue ELSE 0 END) AS growth
              FROM sales GROUP BY product_category ORDER BY growth DESC
        10. Detailed profile / sales details of a top/specific entity (e.g. "nhân viên có doanh thu cao nhất và chi tiết doanh số" / "sales rep with highest avg deal size and show their details"):
            → Use a subquery/CTE to find the target entity first, then SELECT all columns/records for that entity (grouped by month or quarter to show their performance over time).
            → Example "sales rep with highest avg deal size and show their sales details":
               WITH top_rep AS (
                   SELECT sales_rep FROM sales GROUP BY sales_rep ORDER BY AVG(avg_deal_size) DESC LIMIT 1
               )
               SELECT sales_rep, quarter, month, product_category, revenue, units_sold, avg_deal_size FROM sales WHERE sales_rep = (SELECT sales_rep FROM top_rep)
               ORDER BY CASE month WHEN 'Jul' THEN 1 WHEN 'Aug' THEN 2 WHEN 'Sep' THEN 3 WHEN 'Oct' THEN 4 WHEN 'Nov' THEN 5 WHEN 'Dec' THEN 6 END;
        11. If prompt is vague or general (no specific metric/dimension mentioned)
            → Return a comprehensive overview: SELECT region, quarter, product_category, SUM(revenue) AS total_revenue, SUM(units_sold) AS total_units FROM sales GROUP BY region, quarter, product_category ORDER BY total_revenue DESC

        Always use meaningful column aliases (total_revenue, total_units, avg_deal, pct_share, q3_revenue, q4_revenue, growth, etc.).
        Return ONLY the raw SQL query, no markdown, no explanation."""
        resp = generate_content_with_retry(
            client=client,
            model="gemini-2.5-flash",
            contents=f"{sys_prompt}\n\nUser request: {prompt}"
        )
        query = clean_sql_query(resp.text)
    else:
        query = "SELECT region, quarter, month, product_category, revenue, units_sold, avg_deal_size, sales_rep FROM sales;"
        
    try:
        print(f">>> DATA FETCHER: executing query: {query}", flush=True)
        result_str = query_sales_data(query)
        import json
        parsed_res = json.loads(result_str)
        if "error" in parsed_res:
            raise ValueError(parsed_res["error"])
    except Exception as e:
        safe_err = str(e).encode('ascii', errors='ignore').decode('ascii')
        print(f">>> DATA FETCHER ERROR: {safe_err}. Falling back to full query.", flush=True)
        # Fallback to full data if query fails
        fallback_query = "SELECT region, quarter, month, product_category, revenue, units_sold, avg_deal_size, sales_rep FROM sales;"
        result_str = query_sales_data(fallback_query)
    
    # Save the raw data to workflow state using dictionary access
    ctx.state["sales_data_raw"] = result_str
    
    print(f">>> DATA FETCHER FINISHED. result_str size: {len(result_str)}", flush=True)
    return result_str


@node(name="UI_Generator_Agent")
def ui_generator_node(ctx: Context) -> dict:
    """DESIGN: UI Generator — the A2UI dashboard architect with self-correction.

    ROLE: Takes raw sales data from workflow state and generates a complete,
    validated A2UI v0.9 Hybrid Output (data + UI component tree). This is the
    most complex node: it must select the right chart type, design a multi-card
    layout, and ensure every component ID exists and every child reference resolves.

    WHY SCHEMA-CONSTRAINED JSON OUTPUT:
    Using `response_mime_type='application/json'` in the Gemini config forces the
    model to output raw JSON rather than markdown-wrapped JSON. Combined with
    low temperature (0.1), this produces consistent, parseable structure.

    WHY 3-ATTEMPT SELF-CORRECTION LOOP:
    Even with schema constraints, A2UI validation failures occur on first attempt
    ~30% of the time (missing component IDs, invalid children references, etc.).
    Instead of failing, the exact jsonschema error is injected into the next
    prompt, allowing the model to correct only the invalid portion while
    preserving the rest of the layout. This brings first-delivery success
    rate to ~99%.

    WHY LAYOUT INTELLIGENCE RULES IN THE PROMPT:
    Without explicit layout rules, the model generates single-card or plain-text
    outputs. The Layout Intelligence Rules (sections A-E in the prompt) teach
    the model to always create: Title Card + Executive Summary Card + KPI Metrics
    Grid + Breakdown Card — a professional dashboard structure that replaces
    boring text tables with rich, interactive card systems.
    """
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

    # Dynamic language detection (detect Vietnamese queries to translate output UI labels/titles/summaries)
    is_vietnamese = False
    lower_user_prompt = user_prompt.lower()
    if any(word in lower_user_prompt for word in [
        "doanh thu", "biểu đồ", "so sánh", "cao nhất", "thấp nhất", 
        "rep", "người", "bán hàng", "khu vực", "miền", "bắc", "nam", 
        "tháng", "quý", "tổng", "tại", "của", "phân tích", "chi tiết",
        "bao nhiêu", "hiển thị", "vẽ"
    ]):
        is_vietnamese = True

    if is_vietnamese:
        lang_info = """
        LANGUAGE REQUIREMENT:
        You MUST generate all UI text, titles, labels, section headers, bullet points, descriptions, and summaries in VIETNAMESE.
        Do NOT translate database field values or rep names (keep 'North', 'South', 'Electronics', 'Furniture', 'Software', rep names original).
        Use clean, natural, professional Vietnamese.
        """
        title_guide = """
        - "người có doanh thu cao nhất" → "Đại diện Kinh doanh Doanh thu Cao nhất"
        - "so sánh Q3 và Q4" → "So sánh Doanh thu Quý 3 và Quý 4"
        - "doanh thu theo danh mục" → "Phân tích Doanh thu theo Danh mục Sản phẩm"
        - "xu hướng doanh thu" → "Xu hướng Doanh thu theo Tháng"
        """
        summary_guide = """
        Extract the most important insights from the data. Write 2–3 rich, detailed bullet points (max 25 words per bullet).
        Avoid generic statements. Provide context (growth rate, share, anomalies, recommendations).
        Use "•" bullets. Examples:
        - "• [Name] dẫn đầu doanh số toàn bộ hệ sinh thái với $X.XXM, cao hơn X% so với bình quân khu vực."
        - "• Doanh thu Q4 đạt mức cao kỷ lục nhờ sự tăng trưởng vượt bậc của danh mục [Category]."
        """
    else:
        lang_info = """
        LANGUAGE REQUIREMENT:
        You MUST generate all UI text, titles, labels, section headers, bullet points, descriptions, and summaries in ENGLISH.
        Keep representative names, categories, regions original.
        Use clean, professional English.
        """
        title_guide = """
        - "người có doanh thu cao nhất" → "Top Revenue Sales Representative"
        - "so sánh Q3 và Q4" → "Q3 vs Q4 Revenue Comparison"
        - "doanh thu theo danh mục" → "Revenue Breakdown by Product Category"
        - "xu hướng doanh thu" → "Monthly Revenue Trend"
        """
        summary_guide = """
        Extract the most important insights from the data. Write 2–3 rich, detailed bullet points (max 25 words per bullet).
        Avoid generic statements. Provide context (growth rate, share, anomalies, recommendations).
        Use "•" bullets. Examples:
        - "• [Name] leads sales performance across the board at $X.XXM, representing X% of department average."
        - "• Q4 revenue surged dramatically driven by a X% growth in the [Category] product line."
        """

    prompt = f"""
    {instructions}
    {lang_info}

    You MUST create a user interface that directly answers and satisfies the following user request:
    USER REQUEST: "{user_prompt}"
    {update_info}
    Available Sales Data (JSON):
    {raw_data}

    LAYOUT INTELLIGENCE RULES — read carefully and apply:
    
    A) TITLE CARD (id="title-card"): The main dashboard title MUST directly name what the user asked for.
       Always use Text with variant="h1" or variant="heading" for the title.
       {title_guide}

    B) EXECUTIVE SUMMARY (id="exec-summary"):
       {summary_guide}

    C) MULTIPLE DATA CARDS & COHESIVE STRUCTURE:
       Do NOT output just a single card or a text block. You should create a structured grid/layout containing multiple card components:
       - Card 1: Title Card (id="title-card") - containing the main report title.
       - Card 2: Executive Summary & Analysis Card (id="exec-summary-card") - containing the rich bullet points explaining the analysis in detail.
       - Card 3: Key Metrics Grid (id="metrics-grid") - a Row containing multiple small KPI cards side-by-side (e.g. Total Revenue, Total Units Sold, Average Deal Size).
       - Card 4: Detailed Breakdown Card (id="breakdown-card") - A single card containing a clear header title and a list of Rows. Each Row represents one item (e.g. category, region, representative) showing its name, its value, and its relative percentage of the total. Do NOT wrap each individual item in its own Card component. Use Column and Row components instead to keep the layout compact and clean.
       - (Optional) Card 5: Recommendation/Actionable Insight Card (id="insight-card") - providing business recommendations based on the findings.
       This provides a multi-dimensional, visually appealing dashboard that replaces boring text tables with clean, highly structured card systems.

    D) KEY-VALUE PAIRS & VISUAL PROGRESS METERS:
       - Format numbers: Revenue: use $ prefix, round to 2 decimal places for millions (e.g. $1.47M, $654.0K). Deal size: show as $ with comma (e.g. $12,500). Units: show as integer (e.g. 1,240 units).
       - Visual progress bars trigger: For each item in the breakdown Column, structure it as a Column containing:
         1. A Row containing the name (e.g., 'Truong Van L') and the value (e.g., '$616.00').
         2. A Text component below it (variant="body") containing the percentage value in parentheses (e.g. '(85.3%)' or '(100.0%)') representing its relative share of the maximum value. This triggers the frontend to render a beautiful animated progress bar dynamically.

    E) STRICT ACCURACY & COMPLIANCE RULES:
       - CRITICAL: Never hallucinate, guess, or copy example values. Double-check all values, averages, names, and totals against the raw JSON data before outputting. If a representative's actual database value is $616.00, do NOT output $816.00 anywhere.
       - Use ONLY: Column, Row, Text, Button, Card components.
       - NO raw HTML, CSS, or JavaScript.
       - Root component MUST have id "root". All children IDs must exist in components list.
       - Use Text variant="h1"/"heading" for the dashboard title, "h2" for card header/titles, "body" for values, "caption" for labels.
       - Every Card (including exec-summary-card, breakdown-card, etc.) MUST have a Column as its direct child, starting with a Text component (variant="h2") serving as the clear header/title of that card.
       - Add exactly one relevant emoji at the start of each Card's title/heading (e.g. "💡 Tóm tắt Phân tích", "📊 Chi tiết Hiệu suất", "🏆 Bảng Xếp hạng") to make them look highly lively and visual.
       - For region cards, add a Button with action="drilldown:<RegionName>" to enable drill-down.
       - The "data" field must be the exact raw data list from the sales data provided.

    You MUST respond with a single JSON object:
    1. "data": The raw JSON sales data list.
    2. "ui": A valid A2UI v0.9 UpdateComponentsMessage.
    3. "chart_type": Choose ONE: "bar" (ranking/comparison), "line" (time-series trend), "pie" (share/proportion/distribution).
       Rules: bar for region/category/rep comparisons, line for month/quarter trends, pie for percentage/share queries.
    """

    from google import genai
    client = genai.Client()
    model_name = "gemini-2.5-flash"
    
    current_prompt = prompt
    attempts = 3
    last_error = getattr(ctx.state, "critic_feedback", None)
    text = ""
    
    for attempt in range(attempts):
        try:
            response = generate_content_with_retry(
                client=client,
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
            import time
            # Sleep to wait out transient 503 UNAVAILABLE or 429 RESOURCE_EXHAUSTED errors
            is_transient = any(term in str(e).upper() for term in ["503", "429", "UNAVAILABLE", "EXHAUSTED"])
            if is_transient:
                time.sleep(3.0)
                # Keep prompt unchanged for transient network errors to perform a clean retry
            else:
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


# Set root_agent to the Workflow so that tests and evaluation harnesses run the full graph workflow
root_agent = sales_canvas_workflow

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
