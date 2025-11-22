"""Dictionary-based HTTP problems and their OpenAPI descriptions."""

from http import HTTPStatus

from fastapi.responses import JSONResponse

PROBLEM_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "default": "about:blank"},
        "title": {"type": "string"},
        "status": {"type": "integer"},
        "detail": {"type": "string"},
        "code": {"type": "string"},
        "instance": {"type": "string"},
        "correlation_id": {"type": "string"},
    },
    "required": ["type", "title", "status", "detail", "code", "instance"],
    "additionalProperties": False,
}


def problem_response(status, code, detail, path, correlation_id=None):
    problem = {
        "type": "about:blank", "title": HTTPStatus(status).phrase, "status": status,
        "code": code, "detail": detail, "instance": path,
    }
    headers = {"Retry-After": "1"} if status in {429, 503} else {}
    if correlation_id:
        problem["correlation_id"] = correlation_id
        headers["X-Request-ID"] = correlation_id
    return JSONResponse(
        problem, status_code=status,
        media_type="application/problem+json", headers=headers,
    )


def problem_responses(*statuses):
    return {status: {
        "description": HTTPStatus(status).phrase,
        "content": {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}},
    } for status in statuses}
