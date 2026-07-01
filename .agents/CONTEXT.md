# A2UI Data Canvas — Hard Rules (CONTEXT.md)

> **Scope**: This document defines non-negotiable, project-wide constraints for every coding agent working within the `a2ui-data-canvas` project. Violation of any rule below is treated as a **critical failure**.

---

## Rule 1 — No Raw HTML / CSS / JavaScript

> [!CAUTION]
> **NEVER generate raw HTML, CSS, or JavaScript.**

All user-interface output **MUST** be produced as **JSON** conforming to the **A2UI v0.9** specification.

### What this means in practice

- ❌ Do NOT emit `<div>`, `<span>`, `<style>`, `<script>`, or any other HTML tag.
- ❌ Do NOT write inline CSS (`style="..."`) or CSS class selectors.
- ❌ Do NOT write JavaScript code, `<script>` blocks, or JS framework components (React JSX, Vue SFC, Svelte, etc.).
- ✅ **DO** return a valid A2UI v0.9 JSON document describing the UI tree.

### A2UI v0.9 JSON Structure (canonical example)

```json
{
  "version": "0.9",
  "root": {
    "type": "Column",
    "children": [
      {
        "type": "Text",
        "props": {
          "value": "Hello, World!",
          "variant": "heading"
        }
      },
      {
        "type": "Button",
        "props": {
          "label": "Click Me",
          "action": "on_click_handler"
        }
      }
    ]
  }
}
```

---

## Rule 2 — Basic Catalog Components Only

> [!IMPORTANT]
> **Only components from the Basic Catalog are permitted.**

The following is the **exhaustive list** of allowed A2UI v0.9 component types:

| Component | Description |
|-----------|-------------|
| `Column`  | Vertical layout container |
| `Row`     | Horizontal layout container |
| `Text`    | Renders text content (supports `variant`: `heading`, `subheading`, `body`, `caption`, `label`) |
| `Button`  | Interactive button element |
| `Card`    | Surface container with optional elevation and padding |

### Constraints

- Any component type **not listed above** is **forbidden**. Do NOT invent custom component types.
- Layout must be composed exclusively from `Column` and `Row` containers.
- Content must be rendered through `Text` and `Button` elements.
- Grouping and visual separation must use `Card`.
- If a UI requirement cannot be satisfied with the Basic Catalog, **stop and ask the user** for guidance instead of improvising.

---

## Rule 3 — No Hallucinated Business Data

> [!CAUTION]
> **The agent MUST NOT fabricate, invent, or hallucinate business data.**

All business data (metrics, KPIs, records, reports, analytics, user data, financial figures, etc.) **MUST** be retrieved through an **MCP (Model Context Protocol) tool call**.

### What this means in practice

- ❌ Do NOT hard-code sample business values (e.g., `"revenue": 1234567`).
- ❌ Do NOT generate placeholder datasets for demonstration purposes.
- ❌ Do NOT estimate, extrapolate, or synthesize business figures from context.
- ✅ **DO** call the appropriate MCP tool (e.g., `execute_sql`, `execute_sql_read_only`, etc.) to fetch real data.
- ✅ If no MCP tool is available for the requested data, **inform the user** that the data source is not configured and request instructions.

### Acceptable data sources

| Source Type | Example MCP Tool |
|-------------|------------------|
| Cloud SQL   | `cloud-sql/execute_sql_readonly` |
| AlloyDB     | `alloydb-postgresql/execute_sql_read_only` |
| Custom MCP  | Any MCP server tool registered in the project |

### Exceptions

- **Static UI labels and instructional text** are allowed (e.g., `"Total Revenue"` as a column header is fine).
- **Configuration constants** are allowed (e.g., date format strings, locale settings).
- **Synthetic data explicitly requested by the user** for testing purposes is allowed only when the user explicitly states they want mock data.

---

## Enforcement Summary

| # | Rule | Severity | Action on Violation |
|---|------|----------|---------------------|
| 1 | No raw HTML/CSS/JS | 🔴 Critical | Reject and regenerate as A2UI JSON |
| 2 | Basic Catalog only | 🔴 Critical | Reject component; ask user for guidance |
| 3 | No hallucinated data | 🔴 Critical | Halt; request MCP tool or user instruction |

---

## Version History

| Date | Version | Author | Change |
|------|---------|--------|--------|
| 2026-06-28 | 1.0.0 | System Architect | Initial hard rules established |
