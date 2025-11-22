"""Configuration for the preserved Compute services."""

import os
from pathlib import Path
from pydantic import Field, SecretStr
from susi_brain.shared.config.settings import AppSettings


def default_data_path():
    source = Path(__file__).resolve()
    root = source.parents[3]
    return root / "data" if source == root / "src/susi_brain/compute/config.py" else Path("data")


class ComputeSettings(AppSettings):
    openai_api_key: SecretStr = SecretStr("")
    susi_api_key: SecretStr = SecretStr("")
    data_path: Path = Field(default_factory=default_data_path)

    @classmethod
    def from_env(cls, **overrides):
        base = dict(overrides.pop("base", None) or {})
        for environment, field in (("OPENAI_API_KEY", "openai_api_key"),
                                   ("SUSI_API_KEY", "susi_api_key")):
            if environment in os.environ:
                base[field] = os.environ[environment]
        return super().from_env("SUSI_COMPUTE_", base=base, **overrides)
