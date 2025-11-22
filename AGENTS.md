# Project instructions

- Write application interfaces, README files, program comments and docstrings in
  English. Preserve the language of user content and intentional multilingual
  input fixtures; localization must not change protocol identifiers or permissions.
- The Swagger/OpenAPI specification is `susi_space/swagger.yaml`.
- `requirements.txt` is the shared source of runtime dependencies for humans,
  Python packaging, and Docker. Update it whenever library usage changes;
  keep development-only dependencies in the `pyproject.toml` dev extra.
- **Update the Dockerfile whenever new libraries are introduced or existing
  library usage changes.** Keep its dependency installation, base image, and any
  required system libraries in sync with `requirements.txt`. Preserve the minimal
  runtime image and check that new dependencies work in it.
- **Update the Swagger file with every change to the API.** This includes changes
  to endpoints, methods, parameters, request bodies, responses, status codes,
  defaults, and documented behavior. Update the specification in the same change
  as the implementation.
- Keep the API under `/api/v1/` and the default server port at `8042`.
  The local default profile remains anonymous. A separately enabled authenticated native/MCP
  profile operates with verified membership, audience, sources and permissions; missing
  or invalid credentials must never fall back to anonymous. The legacy Flask
  document service remains separate and must not expose protected native data.
- Documents live in `space/` by default. Do not commit user documents.
- The MCP component is `susi_space/mcp.py` (`susi-space-mcp`), a stdio adapter to
  the running Flask API. Keep its `write-new-file`, `read-file`, and
  `search-files` tools on the shared HTTP services; do not duplicate storage or
  bypass path validation and atomic publication. Update README tool contracts
  alongside MCP changes and Swagger alongside corresponding HTTP changes.
- Grep content search is shared through `GET /api/v1/grep`. Pass patterns to
  grep using an argument array and `-e`, never through a shell. Keep grep in the
  Docker image and retain the execution and response-size limits.
- When changing hosted documents as an agent, read `GET /api/v1/text?path=...`
  and submit `base_revision` with the edited `content` to `PUT /api/v1/text`.
  Generate a fresh `request_id` per intentional save and keep the same payload
  for retries, inspect `conflicts`, and resolve marker
  blocks using the returned revision. Use `POST /api/v1/merge` for previews.
  Preserve the actual editing baseline; never substitute a new revision token
  for an unreconciled old draft. This keeps concurrent document edits safe.
- Preserve document paths while preventing traversal outside the storage root.
  Do not follow symbolic links in the document tree.
- Moves use the shared structural lock and durable move journal. Preserve
  document revisions and write receipts when paths change, and remap frontend
  buffers without dropping unsaved edits or racing in-flight autosaves.
- Run `python -m pytest` after changing application behavior or the specification.
- Deletion uses the shared structural lock, retains the last stored revisions,
  and requires recursive=true for non-empty folders. Keep the web confirmation
  dialog and pause affected autosaves before a confirmed delete; discard local
  drafts only after the server reports success. Never delete the space root.
