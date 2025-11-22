"""Export or check the Compute API specification without starting the service."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the saved specification differs.")
    args = parser.parse_args()
    import yaml
    from susi_brain.compute.main import create_compute_app
    from susi_brain.compute.config import ComputeSettings

    schema = create_compute_app(ComputeSettings()).openapi()
    target = ROOT / "src" / "susi_brain" / "compute" / "swagger.yaml"
    if args.check:
        if not target.is_file() or yaml.safe_load(target.read_text()) != schema:
            parser.exit(1, "Compute specification is stale; run scripts/export_compute_openapi.py.\n")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(schema, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
