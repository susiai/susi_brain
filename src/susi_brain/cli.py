"""Native entrypoints; defaults < JSON file < environment < CLI."""

import argparse


def main(argv=None):
    parser = argparse.ArgumentParser(prog="susi-brain")
    commands = parser.add_subparsers(dest="application", required=True)
    compute = commands.add_parser("compute", help="Run the native Compute foundation.")
    compute.add_argument("--config", help="JSON file with server and compute groups.")
    compute.add_argument("--host", help="Bind address; default 127.0.0.1.")
    compute.add_argument("--port", type=int, help="Server port; default 8042.")
    compute.add_argument("--openai_api_key", "--openai-api-key", help="OpenAI API key; defaults to OPENAI_API_KEY.")
    compute.add_argument("--susi_api_key", "--susi-api-key", help="Bearer token required for file mutations; defaults to SUSI_API_KEY.")
    compute.add_argument("--data-path", help="File storage directory; source checkouts default to the project's data/.")
    args = parser.parse_args(argv)

    from pydantic import ValidationError
    from .compute.config import ComputeSettings
    from .shared.config.settings import ServerSettings, read_config

    try:
        data = read_config(args.config) if args.config else {}
        if set(data) - {"server", "compute"} or any(not isinstance(value, dict) for value in data.values()):
            raise ValueError("Configuration groups must be known JSON objects.")
        server = ServerSettings.from_env(
            "SUSI_SERVER_", base=data.get("server"), host=args.host, port=args.port,
        )
        settings = ComputeSettings.from_env(
            base=data.get("compute"), openai_api_key=args.openai_api_key,
            susi_api_key=args.susi_api_key, data_path=args.data_path,
        )
    except (ValidationError, ValueError, OSError):
        parser.error("invalid configuration; check JSON fields, CLI options and SUSI_* variables")

    import uvicorn
    from .compute.main import create_compute_app
    from .shared.http.application import install_startup_hints
    from .shared.observability.logging import configure_logging

    configure_logging()
    app = create_compute_app(settings)
    install_startup_hints(app, "compute", server, examples=("health/ready", "status", "capabilities"))
    uvicorn.run(app, host=server.host, port=server.port, access_log=False)
    return 0


def compute_main():
    import sys
    return main(["compute", *sys.argv[1:]])
