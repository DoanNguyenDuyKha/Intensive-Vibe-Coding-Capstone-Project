"""
MCP Server — Sales Data Provider for a2ui-data-canvas.

This server exposes a `query_sales_data` tool that allows agents to run
read-only SQL (SELECT only) against a local SQLite database containing
simulated regional sales data for Q3 and Q4.

Transport: stdio (suitable for local Antigravity / Gemini CLI integration).

Usage:
    # Standalone test
    uv run python mcp_server.py

    # Antigravity will launch this automatically via mcp_config.json
"""

import json
import os
import re
import sqlite3
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DB_DIR = Path(__file__).parent / "data"
DB_PATH = DB_DIR / "sales.db"

# ---------------------------------------------------------------------------
# Database Initialization
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sales (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    region          TEXT    NOT NULL,
    quarter         TEXT    NOT NULL,
    month           TEXT    NOT NULL,
    product_category TEXT   NOT NULL,
    revenue         REAL    NOT NULL,
    units_sold      INTEGER NOT NULL,
    avg_deal_size   REAL    NOT NULL,
    sales_rep       TEXT    NOT NULL
);
"""

SEED_DATA = [
    # ── Q3 — North ──────────────────────────────────────────────
    ("North", "Q3", "Jul", "Electronics",  125000.00, 320,  390.63, "Nguyen Van A"),
    ("North", "Q3", "Aug", "Electronics",  138500.00, 355,  390.14, "Nguyen Van A"),
    ("North", "Q3", "Sep", "Electronics",  142000.00, 360,  394.44, "Nguyen Van A"),
    ("North", "Q3", "Jul", "Furniture",     87000.00, 145,  600.00, "Tran Thi B"),
    ("North", "Q3", "Aug", "Furniture",     91200.00, 152,  600.00, "Tran Thi B"),
    ("North", "Q3", "Sep", "Furniture",     95800.00, 158,  606.33, "Tran Thi B"),
    ("North", "Q3", "Jul", "Software",     210000.00, 420,  500.00, "Le Van C"),
    ("North", "Q3", "Aug", "Software",     225000.00, 450,  500.00, "Le Van C"),
    ("North", "Q3", "Sep", "Software",     240000.00, 470,  510.64, "Le Van C"),

    # ── Q3 — South ──────────────────────────────────────────────
    ("South", "Q3", "Jul", "Electronics",  110000.00, 280,  392.86, "Pham Thi D"),
    ("South", "Q3", "Aug", "Electronics",  115000.00, 295,  389.83, "Pham Thi D"),
    ("South", "Q3", "Sep", "Electronics",  121000.00, 310,  390.32, "Pham Thi D"),
    ("South", "Q3", "Jul", "Furniture",     68000.00, 110,  618.18, "Hoang Van E"),
    ("South", "Q3", "Aug", "Furniture",     72500.00, 118,  614.41, "Hoang Van E"),
    ("South", "Q3", "Sep", "Furniture",     76000.00, 125,  608.00, "Hoang Van E"),
    ("South", "Q3", "Jul", "Software",     180000.00, 360,  500.00, "Vo Thi F"),
    ("South", "Q3", "Aug", "Software",     192000.00, 380,  505.26, "Vo Thi F"),
    ("South", "Q3", "Sep", "Software",     198000.00, 390,  507.69, "Vo Thi F"),

    # ── Q3 — East ───────────────────────────────────────────────
    ("East",  "Q3", "Jul", "Electronics",   95000.00, 240,  395.83, "Dang Van G"),
    ("East",  "Q3", "Aug", "Electronics",  102000.00, 260,  392.31, "Dang Van G"),
    ("East",  "Q3", "Sep", "Electronics",  108000.00, 275,  392.73, "Dang Van G"),
    ("East",  "Q3", "Jul", "Furniture",     55000.00,  90,  611.11, "Bui Thi H"),
    ("East",  "Q3", "Aug", "Furniture",     58000.00,  95,  610.53, "Bui Thi H"),
    ("East",  "Q3", "Sep", "Furniture",     62000.00, 102,  607.84, "Bui Thi H"),
    ("East",  "Q3", "Jul", "Software",     145000.00, 290,  500.00, "Ngo Van I"),
    ("East",  "Q3", "Aug", "Software",     155000.00, 310,  500.00, "Ngo Van I"),
    ("East",  "Q3", "Sep", "Software",     162000.00, 320,  506.25, "Ngo Van I"),

    # ── Q3 — West ───────────────────────────────────────────────
    ("West",  "Q3", "Jul", "Electronics",  130000.00, 335,  388.06, "Do Thi K"),
    ("West",  "Q3", "Aug", "Electronics",  135000.00, 345,  391.30, "Do Thi K"),
    ("West",  "Q3", "Sep", "Electronics",  140000.00, 355,  394.37, "Do Thi K"),
    ("West",  "Q3", "Jul", "Furniture",     92000.00, 150,  613.33, "Truong Van L"),
    ("West",  "Q3", "Aug", "Furniture",     96000.00, 155,  619.35, "Truong Van L"),
    ("West",  "Q3", "Sep", "Furniture",    100000.00, 162,  617.28, "Truong Van L"),
    ("West",  "Q3", "Jul", "Software",     200000.00, 400,  500.00, "Ly Thi M"),
    ("West",  "Q3", "Aug", "Software",     215000.00, 430,  500.00, "Ly Thi M"),
    ("West",  "Q3", "Sep", "Software",     228000.00, 450,  506.67, "Ly Thi M"),

    # ── Q4 — North ──────────────────────────────────────────────
    ("North", "Q4", "Oct", "Electronics",  155000.00, 395,  392.41, "Nguyen Van A"),
    ("North", "Q4", "Nov", "Electronics",  172000.00, 440,  390.91, "Nguyen Van A"),
    ("North", "Q4", "Dec", "Electronics",  210000.00, 530,  396.23, "Nguyen Van A"),
    ("North", "Q4", "Oct", "Furniture",    102000.00, 168,  607.14, "Tran Thi B"),
    ("North", "Q4", "Nov", "Furniture",    115000.00, 188,  611.70, "Tran Thi B"),
    ("North", "Q4", "Dec", "Furniture",    135000.00, 220,  613.64, "Tran Thi B"),
    ("North", "Q4", "Oct", "Software",     260000.00, 510,  509.80, "Le Van C"),
    ("North", "Q4", "Nov", "Software",     285000.00, 560,  508.93, "Le Van C"),
    ("North", "Q4", "Dec", "Software",     340000.00, 665,  511.28, "Le Van C"),

    # ── Q4 — South ──────────────────────────────────────────────
    ("South", "Q4", "Oct", "Electronics",  132000.00, 338,  390.53, "Pham Thi D"),
    ("South", "Q4", "Nov", "Electronics",  145000.00, 370,  391.89, "Pham Thi D"),
    ("South", "Q4", "Dec", "Electronics",  178000.00, 450,  395.56, "Pham Thi D"),
    ("South", "Q4", "Oct", "Furniture",     82000.00, 134,  611.94, "Hoang Van E"),
    ("South", "Q4", "Nov", "Furniture",     90000.00, 148,  608.11, "Hoang Van E"),
    ("South", "Q4", "Dec", "Furniture",    108000.00, 175,  617.14, "Hoang Van E"),
    ("South", "Q4", "Oct", "Software",     215000.00, 425,  505.88, "Vo Thi F"),
    ("South", "Q4", "Nov", "Software",     238000.00, 470,  506.38, "Vo Thi F"),
    ("South", "Q4", "Dec", "Software",     280000.00, 550,  509.09, "Vo Thi F"),

    # ── Q4 — East ───────────────────────────────────────────────
    ("East",  "Q4", "Oct", "Electronics",  118000.00, 300,  393.33, "Dang Van G"),
    ("East",  "Q4", "Nov", "Electronics",  128000.00, 325,  393.85, "Dang Van G"),
    ("East",  "Q4", "Dec", "Electronics",  158000.00, 400,  395.00, "Dang Van G"),
    ("East",  "Q4", "Oct", "Furniture",     68000.00, 112,  607.14, "Bui Thi H"),
    ("East",  "Q4", "Nov", "Furniture",     75000.00, 122,  614.75, "Bui Thi H"),
    ("East",  "Q4", "Dec", "Furniture",     92000.00, 150,  613.33, "Bui Thi H"),
    ("East",  "Q4", "Oct", "Software",     178000.00, 350,  508.57, "Ngo Van I"),
    ("East",  "Q4", "Nov", "Software",     195000.00, 385,  506.49, "Ngo Van I"),
    ("East",  "Q4", "Dec", "Software",     232000.00, 455,  509.89, "Ngo Van I"),

    # ── Q4 — West ───────────────────────────────────────────────
    ("West",  "Q4", "Oct", "Electronics",  148000.00, 378,  391.53, "Do Thi K"),
    ("West",  "Q4", "Nov", "Electronics",  162000.00, 415,  390.36, "Do Thi K"),
    ("West",  "Q4", "Dec", "Electronics",  198000.00, 500,  396.00, "Do Thi K"),
    ("West",  "Q4", "Oct", "Furniture",    108000.00, 175,  617.14, "Truong Van L"),
    ("West",  "Q4", "Nov", "Furniture",    118000.00, 192,  614.58, "Truong Van L"),
    ("West",  "Q4", "Dec", "Furniture",    140000.00, 228,  614.04, "Truong Van L"),
    ("West",  "Q4", "Oct", "Software",     240000.00, 475,  505.26, "Ly Thi M"),
    ("West",  "Q4", "Nov", "Software",     265000.00, 525,  504.76, "Ly Thi M"),
    ("West",  "Q4", "Dec", "Software",     318000.00, 625,  508.80, "Ly Thi M"),
]


def init_database() -> None:
    """Create the SQLite database and seed it with sample sales data."""
    DB_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    cursor.execute(SCHEMA_SQL)

    # Only seed if table is empty (idempotent).
    cursor.execute("SELECT COUNT(*) FROM sales")
    if cursor.fetchone()[0] == 0:
        cursor.executemany(
            """
            INSERT INTO sales
                (region, quarter, month, product_category,
                 revenue, units_sold, avg_deal_size, sales_rep)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            SEED_DATA,
        )
        conn.commit()
        print(f"[OK] Database seeded with {len(SEED_DATA)} rows -> {DB_PATH}")
    else:
        print(f"[INFO] Database already contains data -> {DB_PATH}")

    conn.close()


# ---------------------------------------------------------------------------
# SQL Validation
# ---------------------------------------------------------------------------

_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH|PRAGMA|VACUUM|REINDEX)\b",
    re.IGNORECASE,
)


def _validate_select_only(sql: str) -> None:
    """Raise ValueError if the SQL is not a pure SELECT statement."""
    stripped = sql.strip().rstrip(";").strip()

    if not stripped.upper().startswith("SELECT"):
        raise ValueError(
            "❌ Only SELECT queries are allowed. "
            f"Received statement starting with: '{stripped.split()[0]}'"
        )

    if _FORBIDDEN_KEYWORDS.search(stripped):
        match = _FORBIDDEN_KEYWORDS.search(stripped)
        raise ValueError(
            f"❌ Forbidden SQL keyword detected: '{match.group()}'. "
            "Only read-only SELECT queries are permitted."
        )

    if ";" in stripped:
        raise ValueError(
            "❌ Multiple statements are not allowed. Send one SELECT query at a time."
        )


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="sales-data-server",
)


@mcp.tool()
def query_sales_data(sql_query: str) -> str:
    """Execute a read-only SQL SELECT query against the sales database.

    The database contains a `sales` table with the following columns:
        - id              (INTEGER)  — Primary key
        - region          (TEXT)     — Sales region: North, South, East, West
        - quarter         (TEXT)     — Fiscal quarter: Q3, Q4
        - month           (TEXT)     — Month name: Jul, Aug, Sep, Oct, Nov, Dec
        - product_category (TEXT)    — Category: Electronics, Furniture, Software
        - revenue         (REAL)     — Total revenue in USD
        - units_sold      (INTEGER)  — Number of units sold
        - avg_deal_size   (REAL)     — Average deal size in USD
        - sales_rep       (TEXT)     — Name of the sales representative

    Example queries:
        SELECT region, SUM(revenue) as total FROM sales GROUP BY region;
        SELECT * FROM sales WHERE quarter = 'Q4' AND region = 'North';
        SELECT product_category, SUM(units_sold) FROM sales GROUP BY product_category;

    Args:
        sql_query: A SQL SELECT statement to execute. Only SELECT is allowed;
                   INSERT, UPDATE, DELETE, DROP, and other write operations
                   will be rejected.

    Returns:
        A JSON string containing the query results as a list of row objects,
        or an error message if the query is invalid.
    """
    # Validate: SELECT only.
    try:
        _validate_select_only(sql_query)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)

    # Execute against SQLite.
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(sql_query)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        result = [dict(zip(columns, row)) for row in rows]
        conn.close()

        return json.dumps(
            {
                "columns": columns,
                "row_count": len(result),
                "data": result,
            },
            ensure_ascii=False,
            indent=2,
        )
    except sqlite3.Error as exc:
        return json.dumps(
            {"error": f"SQLite error: {exc}"},
            ensure_ascii=False,
        )


@mcp.tool()
def get_sales_schema() -> str:
    """Return the schema of the sales database.

    Use this tool to discover table structure and column types before
    writing SQL queries with query_sales_data.

    Returns:
        A JSON string describing the tables and their columns.
    """
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        tables = [row[0] for row in cursor.fetchall()]

        schema: dict = {}
        for table in tables:
            cursor.execute(f"PRAGMA table_info({table})")
            cols = cursor.fetchall()
            schema[table] = {
                "columns": [
                    {
                        "name": col[1],
                        "type": col[2],
                        "nullable": not col[3],
                        "primary_key": bool(col[5]),
                    }
                    for col in cols
                ]
            }

            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            schema[table]["row_count"] = cursor.fetchone()[0]

        conn.close()
        return json.dumps(schema, ensure_ascii=False, indent=2)

    except sqlite3.Error as exc:
        return json.dumps({"error": f"SQLite error: {exc}"}, ensure_ascii=False)


@mcp.tool()
def update_sales_data(sql_query: str) -> str:
    """Execute an UPDATE SQL query against the sales database.
    
    Use this to modify the sales data based on natural language requests.
    Example: UPDATE sales SET revenue = revenue * 1.1 WHERE region = 'North' AND quarter = 'Q4';

    Args:
        sql_query: A SQL UPDATE statement to execute. Only UPDATE is allowed.

    Returns:
        A JSON string containing the number of rows affected, or an error.
    """
    stripped = sql_query.strip().rstrip(";").strip()
    if not stripped.upper().startswith("UPDATE"):
        return json.dumps({"error": "Only UPDATE queries are allowed here."})

    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        cursor.execute(sql_query)
        conn.commit()
        rows_affected = cursor.rowcount
        
        # Also recalculate avg_deal_size for updated rows if revenue or units_sold changed
        cursor.execute("UPDATE sales SET avg_deal_size = revenue / units_sold WHERE units_sold > 0")
        conn.commit()
        
        conn.close()
        return json.dumps({"status": "success", "rows_affected": rows_affected})
    except sqlite3.Error as exc:
        return json.dumps({"error": f"SQLite error: {exc}"})


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Ensure database exists and is seeded before starting MCP server.
    init_database()

    print(f"[START] Starting Sales Data MCP Server (stdio transport)...")
    print(f"   Database: {DB_PATH}")
    print(f"   Tools:    query_sales_data, get_sales_schema")
    print(f"   Press Ctrl+C to stop.\n")

    mcp.run(transport="stdio")
