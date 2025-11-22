"""Native Compute factory and direct file entrypoint."""

if __name__ == "__main__":
    import sys
    from pathlib import Path

    # Locate the source package and delegate startup to the existing CLI.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from susi_brain.cli import compute_main

    raise SystemExit(compute_main())

from .config import ComputeSettings
from .service_adapter import install_service_routes
from .system_service import install_system_routes
from susi_brain.shared.http.application import create_scaffold_app
from susi_brain.shared.http.runtime import ApplicationRuntime

PREFIX = "/api/v1/compute"
PENDING = [
    "resident-model-lifecycle", "provider-selection", "decision-interface",
    "resource-admission-control", "embedding", "speech",
]

DESCRIPTION = (
    "Step 1 connects the unchanged Flask chat, audio and file/search services to the new server. "
    "Original HTTP routes and native aliases invoke the same handlers, with their original behavior and limitations. "
    "Readiness describes application resources; local models load on their first request. "
    "Anonymous local profile; default bind address 127.0.0.1, port 8042. "
    "Authenticated configuration is not supported in this step. "
    "Native operational failures use application/problem+json with a safe X-Request-ID; "
    "native 429 and 503 responses include Retry-After: 1. Flask service responses are passed through unchanged. "
    "Chat requires an OpenAI key; file mutations require a SUSI Bearer token. "
    "Missing or invalid supplied credentials never fall back to anonymous access."
)


def create_compute_app(settings=None, *, resources=None):
    settings = settings if settings is not None else ComputeSettings.from_env()
    runtime = ApplicationRuntime(
        resources if resources is not None else [], settings.readiness_timeout_seconds,
    )
    app = create_scaffold_app(
        "compute", settings, runtime, pending=PENDING, stage="migration-step-1",
        implemented=["health", "status", "capabilities", "offline-docs", "chat",
                     "audio-transcription", "file-storage", "search", "system-metrics"],
        description=DESCRIPTION,
    )
    install_system_routes(app)
    install_service_routes(app)

    @app.get(PREFIX + "/capabilities", operation_id="compute__capabilities",
             responses={200: {"content": {"application/json": {"example": {"tasks": ["chat", "transcription"]}}}}})
    def capabilities() -> dict:
        """List the original inference tasks, served through the Flask adapter."""
        return {"tasks": ["chat", "transcription"]}

    return app
