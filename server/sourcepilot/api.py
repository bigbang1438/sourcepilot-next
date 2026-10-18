"""Local workspace API, uploads and asynchronous research tasks."""

import re
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from sourcepilot.agent import ResearchRunner
from sourcepilot.config import Settings
from sourcepilot.models import OllamaModels
from sourcepilot.retrieval import Retriever
from sourcepilot.store import InputError, Store


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SearchBody(StrictBody):
    query: str = Field(min_length=1, max_length=1500)
    limit: int = Field(default=6, ge=1, le=20)
    documentIds: list[str] | None = Field(default=None, max_length=100)
    mode: Literal["hybrid", "vector", "bm25"] = "hybrid"

    @field_validator("query")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("检索词不能为空")
        return value.strip()


class RunBody(StrictBody):
    question: str = Field(min_length=1, max_length=4000)
    documentIds: list[str] | None = Field(default=None, max_length=100)

    @field_validator("question")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("问题不能为空")
        return value.strip()


def create_app(data_dir=None, models=None):
    settings = Settings.from_env(data_dir)
    store = Store(settings.data_dir)
    models = models or OllamaModels(settings)
    retriever = Retriever(store, models)
    runner = ResearchRunner(store, models, retriever, settings)

    @asynccontextmanager
    async def lifespan(app):
        yield
        runner.close()

    app = FastAPI(title="SourcePilot", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.runner = runner
    counters = defaultdict(deque)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "").lower()
        try:
            parsed_host = urlparse("http://" + host).hostname
        except ValueError:
            parsed_host = None
        if parsed_host not in ("localhost", "127.0.0.1", "::1"):
            return JSONResponse(
                {"code": "HOST_DENIED", "message": "仅接受本机工作区地址。"}, status_code=403
            )
        origin = request.headers.get("origin")
        if origin:
            try:
                parsed = urlparse(origin)
                valid = (
                    parsed.scheme == "http"
                    and parsed.hostname in ("localhost", "127.0.0.1", "::1")
                    and parsed.port in (8099, 5173, 5174, 5199)
                )
            except ValueError:
                valid = False
            if not valid:
                return JSONResponse(
                    {"code": "ORIGIN_DENIED", "message": "不允许来自此网站的请求。"},
                    status_code=403,
                )
        if request.url.path.startswith("/api/") and request.method not in (
            "GET",
            "HEAD",
            "OPTIONS",
        ):
            if request.headers.get("x-requested-with") != "SourcePilot":
                return JSONResponse(
                    {"code": "WRITE_HEADER_REQUIRED", "message": "写请求需要工作区安全标记。"},
                    status_code=403,
                )
            try:
                if int(request.headers.get("content-length", "0")) > 21 * 1024 * 1024:
                    return JSONResponse(
                        {"code": "UPLOAD_TOO_LARGE", "message": "请求最大 21 MB。"}, status_code=413
                    )
            except ValueError:
                return JSONResponse(
                    {"code": "INVALID_LENGTH", "message": "请求长度不合法。"}, status_code=400
                )
        key = (
            request.client.host if request.client else "local",
            "expensive" if request.method == "POST" else "read",
        )
        timestamps = counters[key]
        now = time.monotonic()
        while timestamps and timestamps[0] < now - 60:
            timestamps.popleft()
        if len(timestamps) >= (90 if key[1] == "expensive" else 1200):
            return JSONResponse(
                {"code": "RATE_LIMITED", "message": "请求过于频繁，请稍后再试。"}, status_code=429
            )
        timestamps.append(now)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(InputError)
    async def input_error(request, exc):
        return JSONResponse({"code": "INVALID_INPUT", "message": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse({"code": "NOT_FOUND", "message": str(exc).strip("'")}, status_code=404)

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "chatModel": settings.chat_model,
            "embeddingModel": settings.embedding_model,
            "visionModel": settings.vision_model,
            **models.ready(),
        }

    @app.get("/api/documents")
    def documents():
        return store.documents()

    @app.post("/api/documents")
    def upload(file: Annotated[UploadFile, File()]):
        data = file.file.read(20 * 1024 * 1024 + 1)
        try:
            return store.ingest(file.filename or "", data, models)
        finally:
            file.file.close()

    @app.get("/api/documents/{doc_id}/pages/{page}")
    def page(doc_id: str, page: int, version: int | None = Query(default=None, ge=1)):
        return store.page(doc_id, page, version)

    @app.get("/api/assets/{filename}")
    def asset(filename: str):
        if not re.fullmatch(r"[a-f0-9]{64}\.png", filename):
            raise HTTPException(404, "图片不存在")
        path = store.root / "assets" / filename
        if not path.is_file():
            raise HTTPException(404, "图片不存在")
        return FileResponse(
            path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"}
        )

    @app.post("/api/search")
    def search(body: SearchBody):
        return retriever.search(
            body.query, body.limit, snapshot=store.snapshot(body.documentIds), mode=body.mode
        )

    @app.post("/api/runs")
    def create_run(body: RunBody):
        with runner.lock:
            if len(runner.active) >= 2:
                raise HTTPException(429, "同时最多运行两个任务；取消中的请求也须等待实际退出。")
            run_id = store.create_run(body.question, body.documentIds)
            runner.submit(run_id)
        return {"id": run_id, "status": "running"}

    def public_run(run_id):
        with runner.lock:
            current = store.run(run_id)
            if run_id in runner.active:
                current["usage"] = {
                    **current["usage"],
                    "seconds": round(runner.elapsed({"run_id": run_id}), 3),
                }
            return {
                **current,
                "executionActive": run_id in runner.active,
                "cancellationRequested": bool(store.raw_run(run_id)["cancel"]),
            }

    @app.get("/api/runs")
    def runs():
        return [public_run(row["id"]) for row in store.runs()]

    @app.get("/api/runs/{run_id}")
    def run(run_id: str):
        return public_run(run_id)

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str):
        with runner.lock:
            current = store.run(run_id)
            if current["status"] != "completed":
                store.update_run(
                    run_id, cancel=1, status="cancelled", error="任务已取消；检查点保留，可继续。"
                )
                event = runner.cancel_events.get(run_id)
                if event:
                    event.set()
            return public_run(run_id)

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str):
        try:
            runner.submit(run_id)
        except ValueError as exc:
            raise HTTPException(429, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return public_run(run_id)

    @app.get("/api/runs/{run_id}/report")
    def report(run_id: str):
        store.run(run_id)
        return Response(
            store.report(run_id),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="sourcepilot-{run_id}.md"'},
        )

    static = Path(__file__).resolve().parents[1] / "static"
    static.mkdir(exist_ok=True)
    app.mount("/", StaticFiles(directory=static, html=True, check_dir=False), name="web")
    return app
