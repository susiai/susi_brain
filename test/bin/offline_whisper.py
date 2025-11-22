"""Run local Whisper, or exercise the running Compute services with real inputs."""

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx


def check(client, kind, args):
    prefix = "/api/v1/compute"
    if kind == "status":
        response = client.get(prefix + "/status")
        response.raise_for_status()
        assert response.json()["ready"] is True
        assert response.json()["stage"] == "migration-step-1"
    elif kind == "openapi":
        response = client.get(prefix + "/openapi.json")
        response.raise_for_status()
        assert response.json()["openapi"].startswith("3.")
        assert prefix + "/inference/chat" in response.json()["paths"]
    elif kind == "metrics":
        response = client.get("/api/system/status.json")
        response.raise_for_status()
        assert response.json()["cpu_count"] > 0
    elif kind == "chat":
        payload = {"messages": [{"role": "user", "content": "Hello!"}]}
        if args.chat_model:
            payload["model"] = args.chat_model
        response = client.post("/api/text/chat/completions", json=payload)
        response.raise_for_status()
        assert response.json()["choices"]
        print(json.dumps(response.json(), ensure_ascii=False))
    elif kind == "audio":
        with args.file.open("rb") as audio:
            response = client.post("/api/audio/transcriptions", files={"file": (args.file.name, audio, "audio/wav")}, data={"model": args.model})
        response.raise_for_status()
        assert isinstance(response.json()["text"], str) and response.json()["text"].strip()
        print(json.dumps(response.json(), ensure_ascii=False))
    elif kind == "files":
        if not args.token:
            raise ValueError("Set SUSI_API_KEY or --token to test file mutations.")
        filename = "checks/" + uuid4().hex + "/sample.jsonl"
        route = "/api/data/" + filename
        headers = {"Authorization": "Bearer " + args.token}
        content = b'{"sku":"check","text_t":"file storage smoke test"}\n'
        response = client.post(route, files={"file": ("sample.jsonl", content)}, headers=headers)
        response.raise_for_status()
        try:
            assert client.get(route).content == content
            result = client.post("/api/data/search", json={"file": filename, "query": "smoke test"})
            result.raise_for_status()
            assert result.json() == [{"sku": "check", "text_t": "file storage smoke test"}]
            listing = client.get("/api/data/" + filename.rsplit("/", 1)[0] + "/index.json")
            listing.raise_for_status()
            assert listing.json()["dir"][0]["name"] == "sample.jsonl"
        finally:
            client.delete(route, headers=headers).raise_for_status()
        assert client.get(route).status_code == 404
    print(kind + ": passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["offline", "all", "status", "openapi", "audio", "chat", "files", "metrics"], default="offline", nargs="?")
    parser.add_argument("--base-url", default="http://127.0.0.1:8042")
    parser.add_argument("--file", type=Path, default=Path(__file__).resolve().parents[1] / "wav/test_english_alex_how.wav")
    parser.add_argument("--model", default="tiny")
    parser.add_argument("--chat-model")
    parser.add_argument("--token", default=os.environ.get("SUSI_API_KEY", ""))
    args = parser.parse_args()
    try:
        if args.kind == "offline":
            import whisper
            print(whisper.load_model(args.model).transcribe(str(args.file), fp16=False)["text"])
        else:
            with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=300, trust_env=False) as client:
                for kind in (["status", "openapi", "metrics", "files", "audio", "chat"] if args.kind == "all" else [args.kind]):
                    check(client, kind, args)
    except (OSError, ValueError, AssertionError, httpx.HTTPError) as error:
        parser.exit(1, "Compute check failed: " + str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
