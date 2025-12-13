"""Native Compute factory and direct file entrypoint."""

if __name__ == "__main__":
    import sys
    from pathlib import Path

    # Locate the source package and delegate startup to the existing CLI.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from susi_brain.cli import compute_main

    raise SystemExit(compute_main())

from .config import ComputeSettings
import asyncio
from contextlib import asynccontextmanager
from .api import install_service_routes
from .text_service import ChatCompletion
from .calibration import EndpointCalibration
from .calibration_api import install_calibration_routes
from .system_service import install_system_routes
from .web import install_compute_web_routes
from .monitoring import RequestMonitor, RequestMonitoringMiddleware
from .monitoring_api import install_monitoring_routes
from susi_brain.shared.http.runtime import ApplicationRuntime
from susi_brain.shared.http.application import create_scaffold_app

PREFIX = "/api/v1/compute"
PENDING = [
    "resident-model-lifecycle", "decision-interface",
    "resource-admission-control", "embedding",
]

DESCRIPTION = "API for computational services"


def create_compute_app(settings=None, *, resources=None, chat_transport=None):
    settings = settings if settings is not None else ComputeSettings.from_env()
    runtime = ApplicationRuntime(
        resources if resources is not None else [], settings.readiness_timeout_seconds,
    )
    chat = ChatCompletion(settings, transport=chat_transport)
    calibration = EndpointCalibration(chat)
    monitor = RequestMonitor(settings.request_log_capacity, log_directory=settings.request_log_directory)

    @asynccontextmanager
    async def lifespan(app):
        async with runtime.lifespan(app), chat.lifespan():
            await asyncio.to_thread(monitor.start)
            try:
                yield
            finally:
                try:
                    await calibration.close()
                finally:
                    await asyncio.to_thread(monitor.close)

    app = create_scaffold_app(
        "compute", settings, runtime, pending=PENDING, stage="migration-step-1",
        expose_headers=('X-Compute-Target',),
        implemented=["health", "status", "capabilities", "offline-docs", "chat",
                     "audio-transcription", "speech", "file-storage", "search", "system-metrics", "provider-selection",
                     "endpoint-calibration", "web-interface", "request-monitoring"],
        description=DESCRIPTION,
    )
    app.router.lifespan_context = lifespan
    app.state.calibration = calibration
    app.state.request_monitor = monitor
    app.add_middleware(RequestMonitoringMiddleware, monitor=app.state.request_monitor)
    install_system_routes(app)
    install_service_routes(app, chat, calibration.speech)
    install_calibration_routes(app, calibration)
    install_compute_web_routes(app)
    install_monitoring_routes(app, app.state.request_monitor)

    @app.get(PREFIX + "/capabilities", operation_id="compute__capabilities",
             responses={200: {"content": {"application/json": {"example": {"tasks": ["chat", "transcription", "speech"]}}}}})
    def capabilities() -> dict:
        """List the supported inference tasks."""
        return {"tasks": ["chat", "transcription", "speech"]}

    return app
