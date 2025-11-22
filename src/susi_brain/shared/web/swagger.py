"""Serve the locally packaged Swagger UI for any native application."""

from pathlib import Path

from fastapi import Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

STATIC = Path(__file__).with_name("static")
VENDOR = STATIC / "vendor" / "swagger-ui"


def install_swagger_routes(app, owner):
    """Install this application's documentation using the shared UI assets."""
    prefix = f"/api/v1/{owner}"

    app.mount(
        prefix + "/docs-assets",
        StaticFiles(directory=VENDOR),
        name=owner + "__docs_assets",
    )

    @app.get(prefix + "/docs", response_class=HTMLResponse, operation_id=owner + "__docs")
    def documentation(request: Request):
        """Explore and execute API requests with Swagger UI. JavaScript, CSS and icons are served locally from docs-assets; no CDN or external validator is used. Requests target the current origin and deployment prefix."""
        base = request.scope.get("root_path", "") + prefix
        response = get_swagger_ui_html(
            openapi_url=base + "/openapi.json", title=app.title + " - Swagger UI",
            swagger_js_url=base + "/docs-assets/swagger-ui-bundle.js",
            swagger_css_url=base + "/docs-assets/swagger-ui.css",
            swagger_favicon_url=base + "/docs-assets/favicon-32x32.png",
            swagger_ui_parameters={"validatorUrl": None},
        )
        response.headers.update({
            "Content-Security-Policy": "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
        })
        return response
