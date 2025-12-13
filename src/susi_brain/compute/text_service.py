"""OpenAI chat proxy with configured model groups and live model discovery."""

import asyncio
import json
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from fastapi import Body, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from susi_brain.shared.observability.correlation import outbound_headers
from susi_brain.shared.http.errors import ServiceError
from .routing import ChatRouting, endpoint_id
from .stream_metadata import StreamMetadata

MAX_MODELS_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


async def without_cookies(request):
    """Prevent the shared pool from sending cookies retained from earlier callers."""
    request.headers.pop("cookie", None)


def proxy_error(status, code, message):
    return JSONResponse({"error": {
        "message": message,
        "type": "invalid_request_error" if status < 500 else "server_error",
        "param": "model" if code == "invalid_model" else None,
        "code": code,
    }}, status_code=status, headers={"Retry-After": "1"} if status == 503 else None)


async def bounded_body(response, limit):
    content = bytearray()
    async for chunk in response.aiter_bytes():
        if len(content) + len(chunk) > limit:
            raise ValueError("The upstream response exceeds its size limit.")
        content.extend(chunk)
    return bytes(content)


class ChatStream(StreamingResponse):
    """Annotate the first/terminal JSON lines and close upstream on every exit."""

    def __init__(self, upstream, *, metadata, connection=None, target_header=None):
        self.upstream = upstream
        self.connection = connection
        self.annotations, self.closed = StreamMetadata(metadata), False
        headers = {"Cache-Control": "no-cache, no-store, must-revalidate, no-transform",
                   "Pragma": "no-cache", "Expires": "0", "X-Accel-Buffering": "no",
                   "Content-Type": upstream.headers.get("content-type", "text/event-stream")}
        if "retry-after" in upstream.headers:
            headers["Retry-After"] = upstream.headers["retry-after"]
        if target_header is not None:
            headers['X-Compute-Target'] = target_header
        super().__init__(self.chunks(), media_type="text/event-stream",
                         status_code=upstream.status_code, headers=headers)

    async def chunks(self):
        """Forward middle lines unchanged and stop reading once [DONE] arrives."""
        count = 0
        async for chunk in self.upstream.aiter_bytes():
            count += len(chunk)
            if count > MAX_RESPONSE_BYTES:
                raise ValueError("The upstream stream exceeds its size limit.")
            for output in self.annotations.feed(chunk):
                if self.annotations.done:
                    await self.close_upstream()
                yield output
            if self.annotations.done:
                return
        for output in self.annotations.feed(b'', final=True):
            if self.annotations.done:
                await self.close_upstream()
            yield output

    async def close_upstream(self):
        if self.closed:
            return
        self.closed = True
        try:
            await self.upstream.aclose()
        finally:
            if self.connection is not None:
                self.connection.release()

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.close_upstream()


class ChatCompletion:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.endpoints = settings.chat_endpoints
        self.routing = ChatRouting(settings, self.endpoints)
        self.transport = transport
        self.client = None

    @asynccontextmanager
    async def lifespan(self):
        async with httpx.AsyncClient(
            transport=self.transport, trust_env=False, follow_redirects=False,
            event_hooks={"request": [without_cookies]},
            timeout=httpx.Timeout(self.settings.chat_timeout_seconds,
                                  connect=min(5, self.settings.chat_timeout_seconds)),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        ) as client:
            self.client = client
            try:
                yield
            finally:
                self.client = None

    async def discover_models(self, endpoint):
        """Share authenticated bounded discovery with proxying and calibration."""
        async def read():
            async with self.client.stream(
                "GET", endpoint.url + "/models",
                headers={**outbound_headers(), **endpoint.authorization_headers()},
                timeout=self.settings.model_probe_timeout_seconds,
            ) as response:
                if response.status_code != 200:
                    raise ServiceError(503, f"inventory_http_{response.status_code}", "The model inventory is unavailable.")
                payload = json.loads(await bounded_body(response, MAX_MODELS_BYTES))
                rows = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(rows, list) or any(not isinstance(row, dict)
                        or not isinstance(row.get("id"), str) or not row["id"] for row in rows):
                    raise ValueError("Invalid model inventory.")
                return [row["id"] for row in rows]
        try:
            return await asyncio.wait_for(read(), self.settings.model_probe_timeout_seconds)
        except (httpx.TimeoutException, asyncio.TimeoutError):
            raise ServiceError(503, "inventory_timeout", "Model discovery exceeded its deadline.") from None
        except httpx.HTTPError:
            raise ServiceError(503, "inventory_connection_failed", "The model inventory could not be reached.") from None
        except (ValueError, RecursionError):
            raise ServiceError(503, "inventory_invalid", "The model inventory is invalid or too large.") from None

    async def models(self, endpoint):
        """Treat failed inventories as unavailable for ordinary chat selection."""
        try:
            return await self.discover_models(endpoint)
        except ServiceError:
            return []

    async def post(self, request: Request, data: dict | None = Body(default=None)):
        if not data or data.get("model") not in ("smart", "small"):
            return proxy_error(400, "invalid_model", "Choose model smart or small.")
        if not isinstance(data.get("messages"), list) or not data["messages"] or any(
                not isinstance(message, dict) for message in data["messages"]):
            return proxy_error(400, "invalid_messages", "Provide a nonempty messages array of objects.")
        if "stream" in data and type(data["stream"]) is not bool:
            return proxy_error(400, "invalid_stream", "stream must be a boolean.")
        group = getattr(self.settings, data["model"])
        if self.client is None or not self.endpoints or not group:
            return proxy_error(503, "model_unavailable", "No configured model is available for this group.")

        request_bytes = len(await request.body())
        inventories = await asyncio.gather(*(self.models(endpoint) for endpoint in self.endpoints))
        target = self.routing.select(data['model'], inventories)
        if target is None:
            return proxy_error(503, "model_unavailable", "No configured model is available for this group.")
        endpoint, model, connection = target
        request.state.compute_target = {'group': data['model'], 'model': model, 'endpoint_id': endpoint_id(endpoint)}
        # Freeze the selected pair's sample before upstream I/O or a new calibration run.
        target_header = json.dumps({
            'endpoint_id': endpoint_id(endpoint), 'model': model,
            'expected': self.routing.expected(endpoint, model),
        }, ensure_ascii=True, separators=(',', ':'))
        upstream = None
        try:
            payload = {**data, "model": model}
            upstream = await self.client.send(self.client.build_request(
                "POST", endpoint.url + "/chat/completions", json=payload,
                headers={**outbound_headers(), **endpoint.authorization_headers(),
                         "Accept": "text/event-stream" if data.get("stream") else "application/json"},
            ), stream=True)
            if 300 <= upstream.status_code < 400:
                return proxy_error(502, "upstream_redirect", "The selected chat server returned a redirect.")
            if data.get("stream") and upstream.is_success:
                if upstream.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
                    return proxy_error(502, "invalid_upstream_response", "The chat server did not return an event stream.")
                response = ChatStream(upstream, connection=connection, target_header=target_header, metadata={
                    'modelname': model, 'compute_group': data['model'],
                    'compute_hostname': urlsplit(endpoint.url).hostname, 'request_bytes': request_bytes,
                })
                upstream = None  # The streaming response now owns cleanup.
                connection = None
                return response
            content = await asyncio.wait_for(bounded_body(upstream, MAX_RESPONSE_BYTES),
                                             self.settings.chat_timeout_seconds)
            headers = {name: upstream.headers[name] for name in ("content-type", "retry-after")
                       if name in upstream.headers}
            headers['X-Compute-Target'] = target_header
            return Response(content, status_code=upstream.status_code, headers=headers)
        except (httpx.TimeoutException, asyncio.TimeoutError):
            return proxy_error(504, "upstream_timeout", "The selected chat server timed out.")
        except httpx.HTTPError:
            return proxy_error(502, "upstream_unavailable", "The selected chat server could not complete the request.")
        except ValueError:
            return proxy_error(502, "invalid_upstream_response", "The chat request or upstream response is invalid or too large.")
        finally:
            try:
                if upstream is not None:
                    await upstream.aclose()
            finally:
                if connection is not None:
                    connection.release()
