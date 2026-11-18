"""FastAPI entry point. Single endpoint: POST /link-analysis."""

import json
import logging
import time
from contextlib import asynccontextmanager

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import get_settings
from .schemas import ErrorDetail, ErrorResponse, LinkAnalysisRequest, LinkAnalysisResponse
from .services.analysis_service import analyze_website
from .services.llm_service import LLMConfigError, LLMError
from .services.website_extractor import FetchError
from .utils.validators import UnsafeURLError, validate_public_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 16_384


class RequestBodyLimitMiddleware:
    """Reject oversized bodies before Starlette buffers them into memory."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_REQUEST_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    await self._reject(send)
                    return
                if declared > self.max_bytes:
                    await self._reject(send)
                    return

        received = 0
        buffered = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            received += len(body)
            if received > self.max_bytes:
                await self._reject(send)
                return
            buffered.extend(body)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> dict:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(buffered), "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, replay, send)

    async def _reject(self, send: Send) -> None:
        payload = json.dumps(
            {
                "success": False,
                "error": {
                    "code": "payload_too_large",
                    "message": f"Request body must not exceed {self.max_bytes} bytes.",
                },
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    async with httpx.AsyncClient(
        headers={"User-Agent": settings.user_agent},
        follow_redirects=False,
        trust_env=False,
    ) as client:
        app.state.http_client = client
        yield


app = FastAPI(
    title="AI-Powered Website Link Analysis API",
    version="1.0.0",
    description=(
        "Fetches a public website, understands its content with Google Gemini, and returns "
        "competitor, search-query, SEO, service and target-audience insights as structured JSON."
    ),
    lifespan=lifespan,
)

app.add_middleware(RequestBodyLimitMiddleware, max_bytes=MAX_REQUEST_BYTES)


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    payload = ErrorResponse(error=ErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status_code, content=payload.model_dump())


@app.exception_handler(UnsafeURLError)
async def unsafe_url_handler(request: Request, exc: UnsafeURLError) -> JSONResponse:
    return _error(400, "invalid_url", str(exc))


@app.exception_handler(FetchError)
async def fetch_error_handler(request: Request, exc: FetchError) -> JSONResponse:
    return _error(502, "fetch_failed", str(exc))


@app.exception_handler(LLMConfigError)
async def llm_config_handler(request: Request, exc: LLMConfigError) -> JSONResponse:
    logger.error("LLM not configured: %s", exc)
    return _error(503, "llm_not_configured", str(exc))


@app.exception_handler(LLMError)
async def llm_error_handler(request: Request, exc: LLMError) -> JSONResponse:
    logger.error("LLM failure: %s", exc)
    return _error(502, "llm_failed", str(exc))


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _error(422, "invalid_request", "Request body must be JSON with a 'url' string.")


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error")
    return _error(500, "internal_error", "Unexpected server error.")


@app.post(
    "/link-analysis",
    response_model=LinkAnalysisResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid or unsafe URL"},
        422: {"model": ErrorResponse, "description": "Malformed request body"},
        502: {"model": ErrorResponse, "description": "Website fetch or LLM failure"},
        503: {"model": ErrorResponse, "description": "LLM provider not configured"},
    },
    summary="Analyse a website",
    tags=["analysis"],
)
async def link_analysis(payload: LinkAnalysisRequest, request: Request) -> LinkAnalysisResponse:
    url = payload.url.strip()
    await anyio.to_thread.run_sync(validate_public_url, url, abandon_on_cancel=True)
    safe_url = url.replace("\r", " ").replace("\n", " ")
    logger.info("Analysing %s", safe_url)
    started = time.perf_counter()
    result = await analyze_website(url, request.app.state.http_client)
    logger.info("Analysis completed for %s in %.1fs", safe_url, time.perf_counter() - started)
    return result
