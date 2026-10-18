"""Local FastAPI service: isolated requests, bounded work, opaque chart downloads."""

import hashlib
import hmac
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from openai import APITimeoutError, RateLimitError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.analytics_agent import build_agent, extract_final_text
from agent.observability import invoke_analysis
from tools import sql_tool, visualization_tool

BASE_DIR = Path(__file__).resolve().parents[1]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiSettings:
    token: str = field(default="", repr=False)
    max_active: int = 2
    max_artifact_runs: int = 100
    artifact_ttl_seconds: float = 3600

    def __post_init__(self):
        if not 1 <= self.max_active <= 8 or not 1 <= self.max_artifact_runs <= 1000 or self.artifact_ttl_seconds <= 0:
            raise ValueError("Invalid API resource limits.")

    @classmethod
    def from_env(cls):
        load_dotenv(BASE_DIR / ".env", override=False)
        return cls(token=os.getenv("ANALYTICS_API_TOKEN", ""),
                   max_active=int(os.getenv("ANALYTICS_API_MAX_ACTIVE", "2")))


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, max_length=5000)

    @field_validator("question")
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Question must not be blank.")
        return value


class ChartLink(BaseModel):
    id: UUID
    url: str
    label: str


class AnalysisResponse(BaseModel):
    request_id: UUID
    status: str
    answer: str
    artifacts: list[ChartLink]
    metrics: dict


class BusyError(Exception):
    pass


class ModelNotConfigured(Exception):
    pass


def default_agent_factory():
    if not os.getenv("OPENAI_API_KEY"):
        raise ModelNotConfigured()
    # No checkpointer: API callers cannot choose or read another conversation.
    return build_agent()


class ArtifactRegistry:
    """Bounded process-local index. Files remain owned by the chart tool."""

    def __init__(self, root, settings, clock=time.monotonic):
        self.root = Path(root).resolve()
        self.settings = settings
        self.clock = clock
        self.runs = OrderedDict()
        self.lock = threading.RLock()

    def _prune(self):
        now = self.clock()
        for key in list(self.runs):
            if now - self.runs[key][0] >= self.settings.artifact_ttl_seconds:
                del self.runs[key]
        while len(self.runs) > self.settings.max_artifact_runs:
            self.runs.popitem(last=False)

    def _read(self, path):
        path = Path(path)
        if path.is_symlink() or not path.resolve().is_relative_to(self.root) or path.suffix.lower() != ".png":
            raise ValueError("Invalid chart artifact.")
        if not path.is_file() or path.stat().st_size > 5_000_000:
            raise ValueError("Invalid chart artifact.")
        data = path.read_bytes()
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Invalid chart artifact.")
        return data

    def register(self, request_id, paths):
        entries, links = {}, []
        for path in dict.fromkeys(paths):
            data = self._read(path)
            artifact_id = uuid4()
            entries[artifact_id] = (Path(path).resolve(), hashlib.sha256(data).hexdigest())
            links.append(ChartLink(id=artifact_id, label=f"Chart {len(links) + 1}",
                url=f"/v1/analyses/{request_id}/artifacts/{artifact_id}"))
        with self.lock:
            self._prune()
            if entries:
                self.runs[request_id] = (self.clock(), entries)
                self._prune()
        return links

    def download(self, request_id, artifact_id):
        with self.lock:
            self._prune()
            entry = self.runs.get(request_id, (None, {}))[1].get(artifact_id)
        if entry is None:
            raise FileNotFoundError()
        path, expected_hash = entry
        try:
            data = self._read(path)
        except (OSError, ValueError):
            raise FileNotFoundError() from None
        if hashlib.sha256(data).hexdigest() != expected_hash:
            raise FileNotFoundError()
        # Return verified bytes, rather than reopening a path after verification.
        return data


class AnalysisRuntime:
    def __init__(self, settings, agent_factory, invoker, output_dir):
        self.factory, self.invoker = agent_factory, invoker
        self.agent = None
        self.build_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(settings.max_active)
        self.artifacts = ArtifactRegistry(output_dir, settings)

    def analyze(self, question, request_id):
        if not self.slots.acquire(blocking=False):
            raise BusyError()
        try:
            with self.build_lock:
                if self.agent is None:
                    self.agent = self.factory()
            result = self.invoker(self.agent, {"messages": [{"role": "user", "content": question}]},
                config={"run_id": request_id, "recursion_limit": 40,
                        "configurable": {"thread_id": str(request_id)}})
            status = result.get("status")
            if status not in {"completed", "responded", "failed"}:
                raise ValueError("Unexpected workflow status.")
            links = self.artifacts.register(request_id, result.get("artifacts", []))
            answer = extract_final_text(result)
            for path, link in zip(dict.fromkeys(result.get("artifacts", [])), links):
                answer = answer.replace(str(path), link.url)
            return AnalysisResponse(request_id=request_id, status=status, answer=answer,
                                    artifacts=links, metrics=result.get("metrics", {}))
        finally:
            self.slots.release()


def database_readable(path):
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("SELECT 1 FROM transactions LIMIT 1").fetchone()
        return True
    except (OSError, sqlite3.Error):
        return False


def create_app(*, settings=None, agent_factory=default_agent_factory,
               invoker=invoke_analysis, output_dir=None):
    settings = settings or ApiSettings.from_env()
    runtime = AnalysisRuntime(settings, agent_factory, invoker,
                              output_dir or visualization_tool.OUTPUT_DIR)
    app = FastAPI(title="AI Analytics Agent", version="2.0.0",
                  description="Local analytics API. Each request starts an independent analysis.")
    app.state.runtime = runtime
    bearer = HTTPBearer(auto_error=False)

    def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if settings.token:
            received = credentials.credentials if credentials else ""
            if not hmac.compare_digest(received.encode(), settings.token.encode()):
                raise HTTPException(status_code=401, detail="Authentication required.",
                                    headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # The default validation response echoes request values. Keep errors generic.
        return JSONResponse(status_code=422, content={"detail": "Invalid request. Supply a question of 1-5000 characters and no extra fields."})

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        checks = {"model_key_configured": bool(os.getenv("OPENAI_API_KEY")),
                  "database_readable": database_readable(sql_tool.DB_PATH)}
        return JSONResponse(status_code=200 if all(checks.values()) else 503,
                            content={"ready": all(checks.values()), "checks": checks})

    @app.post("/v1/analyses", response_model=AnalysisResponse, dependencies=[Depends(authenticate)])
    def analyze(request: AnalysisRequest):
        request_id = uuid4()
        try:
            return runtime.analyze(request.question, request_id)
        except BusyError:
            raise HTTPException(status_code=429, detail="Analysis capacity is full. Retry later.",
                                headers={"Retry-After": "5"}) from None
        except ModelNotConfigured:
            raise HTTPException(status_code=503, detail="Model credentials are not configured.") from None
        except (APITimeoutError, TimeoutError):
            raise HTTPException(status_code=504, detail="Analysis timed out. Retry with a narrower question.") from None
        except RateLimitError:
            raise HTTPException(status_code=429, detail="Model service rate limit reached. Retry later.",
                                headers={"Retry-After": "5"}) from None
        except Exception as error:
            logger.warning("analysis_failed request_id=%s error_type=%s", request_id, type(error).__name__)
            raise HTTPException(status_code=502, detail="Analysis could not finish. Retry later.") from None

    @app.get("/v1/analyses/{request_id}/artifacts/{artifact_id}", dependencies=[Depends(authenticate)])
    def chart(request_id: UUID, artifact_id: UUID):
        try:
            data = runtime.artifacts.download(request_id, artifact_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Chart not found or expired.") from None
        return Response(content=data, media_type="image/png",
                        headers={"Content-Disposition": f'inline; filename="chart-{artifact_id}.png"',
                                 "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

    return app


app = create_app()
