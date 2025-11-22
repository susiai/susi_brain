"""Native application bootstrap, request correlation and operational routes."""

import logging
import re
from contextlib import asynccontextmanager
from http import HTTPStatus
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException

from susi_brain.shared.observability.correlation import correlation_scope
from susi_brain.shared.web.swagger import install_swagger_routes
from .errors import ServiceError
from .responses import PROBLEM_SCHEMA, problem_response, problem_responses

HEALTH_RESPONSE = {200: {"content": {"application/json": {"example": {"status": "ok"}}}}}


def install_startup_hints(app, owner, server, *, examples):
    """Log local documentation and example URLs after application startup."""
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(server.host, server.host)
    if ":" in host:
        host = f"[{host}]"
    base = f"http://{host}:{server.port}/api/v1/{owner}"
    lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def startup(app):
        async with lifespan(app) as state:
            logger = logging.getLogger("uvicorn.error")
            logger.info("Open %s/docs for Swagger UI.", base)
            for path in examples:
                logger.info("curl '%s/%s'", base, path)
            yield state

    app.router.lifespan_context = startup


class CorrelationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        raw = dict(scope.get("headers", [])).get(b"x-request-id", b"")
        incoming = raw.decode("ascii") if raw.isascii() else ""
        correlation = incoming if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", incoming) else str(uuid4())
        scope.setdefault("state", {})["correlation_id"] = correlation

        async def correlated_send(message):
            if message["type"] == "http.response.start":
                headers = [(key, value) for key, value in message.get("headers", []) if key.lower() != b"x-request-id"]
                message = {**message, "headers": [*headers, (b"x-request-id", correlation.encode())]}
            await send(message)

        with correlation_scope(correlation):
            await self.app(scope, receive, correlated_send)


class SafeFailureMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = False

        async def observed_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        except Exception:
            correlation = scope.get("state", {}).get("correlation_id")
            logging.getLogger("susi_brain").error(
                "", extra={"event": "request_failed", "code": "internal_error", "correlation_id": correlation},
            )
            if started:
                raise RuntimeError("An internal streaming operation failed.") from None
            response = problem_response(
                500, "internal_error", "An internal operation failed.", scope["path"], correlation,
            )
            await response(scope, receive, send)


def install_native_errors(app):
    app.add_middleware(SafeFailureMiddleware)
    app.add_middleware(CorrelationMiddleware)

    @app.exception_handler(ServiceError)
    async def service_error(request, error):
        return problem_response(error.status, error.code, error.detail, request.url.path, request.state.correlation_id)

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        response = problem_response(error.status_code, "http_error", HTTPStatus(error.status_code).phrase, request.url.path, request.state.correlation_id)
        if error.headers:
            response.headers.update(error.headers)
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return problem_response(422, "invalid_request", "Required request fields are missing or invalid.", request.url.path, request.state.correlation_id)


def install_operational_routes(app, owner, status_provider, status_example):
    prefix = f"/api/v1/{owner}"

    @app.get(prefix + "/status", operation_id=owner + "__status",
             responses={200: {"content": {"application/json": {"example": status_example}}}})
    async def status() -> dict:
        """Report service, stage, ready, authentication, protocol_version, implemented/pending features and dependencies. Each dependency has name, required and ready fields. Lifecycle readiness describes application resources independently of individual capabilities."""
        return await status_provider()

    @app.get(prefix + "/openapi.json", include_in_schema=False)
    def openapi_document(request: Request):
        return {**app.openapi(), "servers": [{"url": request.scope.get("root_path") or "/"}]}

    install_swagger_routes(app, owner)


def create_scaffold_app(
    owner, settings, runtime, *, pending=(), stage="scaffold",
    implemented=("health", "status", "offline-docs"), description=None,
):
    """Compose common HTTP behavior; application-specific routes stay with their owner."""
    prefix = f"/api/v1/{owner}"
    app = FastAPI(
        title="SUSI " + owner.title(), version="0.1.0", lifespan=runtime.lifespan,
        description=description or "Native application health, status and locally hosted API documentation.",
        openapi_url=None, docs_url=None, redoc_url=None,
        servers=[{"url": "/"}],
        responses=problem_responses(404, 405, 422, 500),
    )
    app.state.settings = settings
    app.state.runtime = runtime
    install_native_errors(app)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST", "DELETE"],
                       allow_headers=["*"], expose_headers=["X-Request-ID"])

    @app.get(prefix + "/health/live", operation_id=owner + "__health_live", responses=HEALTH_RESPONSE)
    def live() -> dict:
        """The HTTP process is alive; application resources may still be unavailable."""
        return {"status": "ok"}

    @app.get(prefix + "/health/ready", responses={**HEALTH_RESPONSE, **problem_responses(503)}, operation_id=owner + "__health_ready")
    async def ready() -> dict:
        """Required application resources are ready for requests."""
        healthy, _ = await runtime.readiness()
        if not healthy:
            raise ServiceError(503, "not_ready", "A required application resource is unavailable.")
        return {"status": "ok"}

    status_example = {
        "service": owner, "stage": stage, "ready": True,
        "authentication": "disabled", "protocol_version": "1",
        "implemented": list(implemented),
        "pending": list(pending), "dependencies": [],
    }

    async def service_status():
        healthy, dependencies = await runtime.readiness()
        return {**status_example, "ready": healthy, "dependencies": dependencies}

    install_operational_routes(app, owner, service_status, status_example)
    original_openapi = app.openapi

    def openapi():
        schema = original_openapi()
        schema["security"] = []
        schema.setdefault("components", {}).setdefault("schemas", {})["Problem"] = PROBLEM_SCHEMA
        for methods in schema["paths"].values():
            for operation in methods.values():
                correlation_parameter = {
                    "name": "X-Request-ID", "in": "header", "required": False,
                    "description": "Safe ASCII correlation ID; missing or invalid values are replaced by a UUID. This is not an inference or idempotency ID.",
                    "schema": {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,64}$"},
                }
                parameters = operation.setdefault("parameters", [])
                if not any(parameter.get("in") == "header" and parameter.get("name") == "X-Request-ID" for parameter in parameters):
                    parameters.append(correlation_parameter)
                for status, response in operation["responses"].items():
                    headers = response.setdefault("headers", {})
                    headers["X-Request-ID"] = {
                        "description": "The accepted or generated request correlation ID.",
                        "schema": {"type": "string"},
                    }
                    if status in {"429", "503"}:
                        headers["Retry-After"] = {"schema": {"type": "string", "enum": ["1"]}}
        return schema

    app.openapi = openapi
    return app
