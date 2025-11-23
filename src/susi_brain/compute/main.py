"""Native Compute factory and direct file entrypoint."""

if __name__ == "__main__":
    import sys
    from pathlib import Path

    # Locate the source package and delegate startup to the existing CLI.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from susi_brain.cli import compute_main

    raise SystemExit(compute_main())

from .config import ComputeSettings
from .api import install_service_routes
from .system_service import install_system_routes
from susi_brain.shared.http.runtime import ApplicationRuntime
from susi_brain.shared.http.application import create_scaffold_app

PREFIX = "/api/v1/compute"
PENDING = [
    "resident-model-lifecycle", "provider-selection", "decision-interface",
    "resource-admission-control", "embedding", "speech",
]

DESCRIPTION = "API for computational services"


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
        """List the supported inference tasks."""
        return {"tasks": ["chat", "transcription"]}

    return app
