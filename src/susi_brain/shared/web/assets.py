"""Explicitly published, version-pinned terminal theme assets."""

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

STATIC = Path(__file__).with_name('static')
WEBTUI = 'webtui-0.1.10/'
FONT = 'ibm-plex-mono-2.5.0/'
MODULES = ('base.css', 'utils/box.css', *(f'components/{name}.css' for name in
           ('button', 'typography', 'input', 'textarea', 'checkbox', 'table', 'pre', 'progress')))
PUBLIC_ASSETS = {
    'amber.css': (STATIC / 'amber.css', 'text/css'),
    **{WEBTUI + name: (STATIC / 'vendor' / WEBTUI / name, 'text/css') for name in MODULES},
    **{FONT + f'IBMPlexMono-{weight}.woff2':
       (STATIC / 'vendor' / FONT / f'IBMPlexMono-{weight}.woff2', 'font/woff2')
       for weight in ('Regular', 'Medium')},
}


def install_theme_assets(app, owner):
    @app.get(f'/api/v1/{owner}/ui-assets/{{asset_path:path}}',
             operation_id=owner + '__theme_asset', tags=['Interface'],
             responses={200: {'description': 'Local WebTUI 0.1.10 CSS, Amber theme or IBM Plex Mono 2.5.0 font',
                              'content': {'text/css': {'schema': {'type': 'string'}},
                                          'font/woff2': {'schema': {'type': 'string', 'format': 'binary'}}}},
                        404: {'description': 'Asset is not in the packaged public allowlist'}})
    def theme_asset(asset_path: str):
        """Serve only the explicit local theme asset allowlist. Includes WebTUI base, box utilities and components, the custom Amber palette, and regular/medium IBM Plex Mono fonts. No CDN, package manager or frontend runtime is required. Deployment prefixes are resolved by relative CSS imports. Unknown paths and traversal return 404; application files and configuration are never exposed."""
        asset = PUBLIC_ASSETS.get(asset_path)
        if asset is None:
            raise HTTPException(status_code=404, detail='Theme asset not found.')
        path, media = asset
        return FileResponse(path, media_type=media, headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
