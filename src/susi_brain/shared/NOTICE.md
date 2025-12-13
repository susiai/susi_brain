# Third-party material

The incremental SSE decoder in `http/sse.py` was adapted in the prepared SUSI
Brain project from Project Euler LLM Benchmark by Michael Christen. Its Apache
2.0 notice is retained in the source file.

The unchanged `compute/fixtures/vision-42.png` image and the calibration probe
ideas originate from Project Euler LLM Benchmark, commit
`f020b7df0a8e6208f03363915b19418c14a370a9`, by Michael Christen.

The Apache License 2.0 is included in `licenses/Apache-2.0.txt`.

Compute's `stream_metadata.py` adapts the routing annotation strategy and exact
`modelname`, `compute_group`, `compute_hostname`, `request_bytes` wire fields
from the sibling `triton_llmlb` project's `src/stream_passthrough.py` and the
`write_to_downstream` handler in `src/llmlb.py`. The asynchronous byte-line
adapter, limits, HTTPX cleanup and Compute integration are local implementations.
Middle stream data and upstream usage remain unmodified; no sibling background
analysis worker, runtime-ranking policy or response harmonization is imported.

The interface shell (`web/static/index.html`), original base stylesheet
(`web/static/interface.css`) and original monochrome Swagger theme
(`web/static/swagger.css`) are adapted from the prepared `susi_brain_ready`
project's shared SUSI interface. Compute-specific rendering uses this project's
existing calibration and connection services. The per-endpoint grouping and
model-summary arrangement were inspired by `llm-lab/gateway/ollama-dashboard.html`;
no Ollama provisioning controls or browser-to-provider requests are included.

The Amber interface vendors unmodified CSS modules from `@webtui/css` 0.1.10
(MIT), copyright WebTUI, and regular/medium IBM Plex Mono font files from
`@ibm/plex-mono` 2.5.0 (SIL Open Font License 1.1), copyright IBM Corp.
Package versions, module lists and npm archive integrity hashes are recorded in
`web/static/vendor/manifest.json`. Licenses are included as
`licenses/WebTUI-MIT.txt` and `licenses/IBM-Plex-OFL-1.1.txt`. The custom Amber
palette, classless application layout and Swagger overrides are local code.
