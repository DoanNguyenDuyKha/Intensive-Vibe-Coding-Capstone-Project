import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Add the workspace root to sys.path so we can import from app
WORKSPACE_ROOT = Path(__file__).parent.parent.resolve()
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import sales_canvas_workflow

app = FastAPI(title="A2UI Data Canvas Dashboard")


class PromptRequest(BaseModel):
    prompt: str


@app.get("/")
def read_root():
    """Serve the index.html file."""
    index_path = Path(__file__).parent / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(str(index_path))


@app.post("/api/generate")
def generate_canvas(request: PromptRequest):
    """Run the ADK 2.0 Graph Workflow and return the Hybrid Output JSON."""
    try:
        session_service = InMemorySessionService()
        session = session_service.create_session_sync(user_id="user_id", app_name="canvas")
        runner = Runner(agent=sales_canvas_workflow, session_service=session_service, app_name="canvas")

        # Build the user message using types.Content
        user_message = types.Content(
            role="user",
            parts=[types.Part.from_text(text=request.prompt)]
        )

        # Execute the workflow
        events = list(runner.run(
            user_id="user_id",
            session_id=session.id,
            new_message=user_message
        ))

        # Look for the final hybrid output saved in session state
        final_output = session.state.get("final_output")

        if not final_output:
            # Fallback to checking workflow node events outputs
            for event in events:
                if event.output and isinstance(event.output, dict) and "ui" in event.output:
                    final_output = event.output
                    break

        if not final_output:
            raise ValueError("Workflow executed successfully but did not produce a valid Hybrid Output.")

        return final_output

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    # Allow running locally using: uv run python canvas_dashboard/main.py
    print("🚀 Starting FastAPI Server for A2UI Data Canvas Dashboard...")
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
