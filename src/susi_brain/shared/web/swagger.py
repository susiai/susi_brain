"""Serve the locally packaged Swagger UI for any native application."""

import html
from pathlib import Path

from fastapi import Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from susi_brain.shared.web.assets import install_theme_assets

STATIC = Path(__file__).with_name("static")
VENDOR = STATIC / "vendor" / "swagger-ui"


def install_swagger_routes(app, owner):
    """Install this application's documentation using the shared UI assets."""
    prefix = f"/api/v1/{owner}"
    install_theme_assets(app, owner)

    @app.get(prefix + '/docs-assets/theme.css', response_class=FileResponse,
             operation_id=owner + '__swagger_theme',
             responses={200: {'content': {'text/css': {'schema': {'type': 'string'}}}}})
    def theme():
        """Local Amber terminal Swagger theme shared with the SUSI application interfaces. Uses packaged IBM Plex Mono fonts and semantic status colors."""
        return FileResponse(STATIC / 'swagger.css', media_type='text/css', headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    app.mount(
        prefix + "/docs-assets",
        StaticFiles(directory=VENDOR),
        name=owner + "__docs_assets",
    )

    @app.get(prefix + "/docs", response_class=HTMLResponse, operation_id=owner + "__docs")
    def documentation(request: Request):
        """Explore and execute API requests with Swagger UI in the local Amber terminal design. JavaScript, CSS, theme and icons are served locally from docs-assets, with shared fonts/theme in ui-assets; no CDN or external validator is used. Requests target the current origin and deployment prefix. Compute includes a link back to its operational interface."""
        base = request.scope.get("root_path", "") + prefix
        response = get_swagger_ui_html(
            openapi_url=base + "/openapi.json", title=app.title + " - Swagger UI",
            swagger_js_url=base + "/docs-assets/swagger-ui-bundle.js",
            swagger_css_url=base + "/docs-assets/swagger-ui.css",
            swagger_favicon_url=base + "/docs-assets/favicon-32x32.png",
            swagger_ui_parameters={"validatorUrl": None, "persistAuthorization": False,
                                   "displayRequestDuration": True, "defaultModelsExpandDepth": -1},
        )
        body = response.body.decode('utf-8')
        body = body.replace('<html>', '<html data-webtui-theme="amber">')
        body = body.replace('</head>', '<link rel="stylesheet" href="' + html.escape(base, quote=True) + '/docs-assets/theme.css"></head>')
        interface = ('<a href="' + html.escape(base, quote=True) + '/ui">[interface]</a> ' if owner == 'compute' else '')
        header = '<header><h1>' + html.escape(app.title) + ' / Swagger</h1>' + interface + '<a href="' + html.escape(base, quote=True) + '/openapi.json">[OpenAPI]</a></header>'
        response = HTMLResponse(body.replace('<body>', '<body>' + header))
        response.headers.update({
            "Content-Security-Policy": "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
        })
        return response
