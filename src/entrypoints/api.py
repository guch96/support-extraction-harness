import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from harness.contracts import ServiceError
from infrastructure.model import configured_model
from infrastructure.records import FixtureSource
from infrastructure.sqlite import identifier
from support_extraction.benchmark import BENCHMARK_RECORD_IDS, FIXTURE_PATH
from support_extraction.service import ExtractionService, InputError, Submission


class ExtractionRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    text: str | None = None
    record_id: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    new_task: bool = False


def create_app(
    *,
    directory=None,
    model_factory=None,
    ui=False,
    max_source_chars=20000,
    source_reader=None,
    allowed_records=BENCHMARK_RECORD_IDS,
):
    directory = directory or os.getenv("HARNESS_DATA_DIR", "runtime")
    model_factory = model_factory or configured_model
    source_reader = source_reader or FixtureSource(FIXTURE_PATH, allowed_records)

    @asynccontextmanager
    async def lifespan(app):
        app.state.service = await ExtractionService.open(
            Path(directory),
            model_factory,
            max_source_chars=max_source_chars,
            source_reader=source_reader,
            allowed_records=allowed_records,
        )
        try:
            yield
        finally:
            await app.state.service.close()

    app = FastAPI(title="Support Extraction Harness", lifespan=lifespan)

    @app.exception_handler(InputError)
    async def input_error(request, exc):
        status = {"SESSION_EXPIRED": 410, "SESSION_NOT_FOUND": 404,
                  "TASK_NOT_FOUND": 404, "TASK_BUSY": 409, "SOURCE_TOO_LARGE": 413,
                  "ACCESS_DENIED": 403, "REVIEW_NOT_FOUND": 404}.get(exc.code, 422)
        return JSONResponse(status_code=status,
                            content={"error_code": exc.code, "request_id": exc.request_id})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=422,
                            content={"error_code": "INVALID_INPUT", "request_id": identifier()})

    @app.exception_handler(ServiceError)
    async def service_error(request, exc):
        return JSONResponse(status_code=500, content={"error_code": exc.code,
                            "request_id": exc.request_id, "source_saved": exc.source_saved})

    @app.post("/extractions")
    async def extract(body: ExtractionRequest, request: Request):
        return await request.app.state.service.submit(Submission(**body.model_dump()))

    @app.get("/sessions/{session_id}")
    async def restore(session_id: str, request: Request):
        return await request.app.state.service.restore(session_id)

    @app.delete("/sessions/{session_id}", status_code=204)
    async def clear(session_id: str, request: Request):
        await request.app.state.service.clear(session_id)
        return Response(status_code=204)

    @app.get("/reviews")
    async def reviews(request: Request):
        return await request.app.state.service.reviews()

    @app.get("/reviews/{review_id}")
    async def review(review_id: str, request: Request, include_raw: bool = False):
        return await request.app.state.service.review(review_id, raw=include_raw)

    if ui:
        from entrypoints.ui import mount_ui

        mount_ui(app, lambda: app.state.service)

    return app
