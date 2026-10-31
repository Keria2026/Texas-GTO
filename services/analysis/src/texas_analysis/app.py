import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .models import HealthResponse

DEFAULT_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")


def create_app(allowed_origins: tuple[str, ...] | None = None) -> FastAPI:
    if allowed_origins is None:
        configured = os.getenv("TEXAS_ALLOWED_ORIGINS")
        allowed_origins = (
            tuple(origin.strip() for origin in configured.split(",") if origin.strip())
            if configured is not None
            else DEFAULT_ORIGINS
        )
    if any("*" in origin or origin == "null" for origin in allowed_origins):
        raise ValueError("Origins must be explicit; wildcards and null are not allowed")

    app = FastAPI(
        title="Texas GTO Analysis",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed_origins),
        allow_methods=["GET"],
        allow_headers=[],
        allow_credentials=False,
    )

    @app.middleware("http")
    async def validate_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            return JSONResponse(status_code=403, content={"detail": "Origin is not allowed"})
        return await call_next(request)

    @app.get("/v1/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        # Only public service metadata is exposed. No analysis or private data yet.
        return HealthResponse()

    return app
