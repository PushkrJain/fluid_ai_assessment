"""FastAPI entry point: routes and safe error mapping only. Logic lives in agent.py."""
import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from . import llm
from .agent import run_agent
from .document import DOCX_MIME, FILENAME_RE, get_output_dir
from .schemas import AgentRequest, AgentResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("agent.api")

app = FastAPI(title="Autonomous AI Document Agent", version="1.0.0")


@app.get("/health")
def health() -> dict[str, str]:
    try:
        llm.get_settings()
        configured = "configured"
    except llm.LLMConfigError:
        configured = "not_configured"
    return {"status": "ok", "llm": configured}


@app.post("/agent", response_model=AgentResponse)
def create_document(body: AgentRequest):
    # Sync endpoint: FastAPI runs it in a worker thread, so a long LLM run does not block the loop.
    try:
        result = run_agent(body.request)
    except Exception:
        logger.exception("unhandled_error")  # details stay in the server log
        raise HTTPException(status_code=500, detail="Internal error while processing the request")
    if result.status == "failed":
        return JSONResponse(status_code=503, content=result.model_dump(mode="json"))
    return result


@app.get("/documents/{filename}")
def download_document(filename: str):
    # Only server-generated names (<32 hex>.docx) are accepted; user input never forms a path.
    if not FILENAME_RE.fullmatch(filename):
        raise HTTPException(status_code=400, detail="Invalid document name")
    output_dir = get_output_dir()
    path = (output_dir / filename).resolve()
    if path.parent != output_dir or not path.is_file():
        raise HTTPException(status_code=404, detail="Document not found")
    return FileResponse(path, media_type=DOCX_MIME, filename=filename)
