#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "${COMPUTE_PYTHON:-python3.12}" "$script_dir/offline_whisper.py" chat "$@"
