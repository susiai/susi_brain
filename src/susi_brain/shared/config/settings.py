"""Small configuration foundation; no models, credentials or persistence."""

import json
import os
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field

CONFIG_LIMIT = 64 * 1024


class EnvironmentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)

    @classmethod
    def from_env(cls, prefix, *, base=None, **overrides):
        values = dict(base or {})
        for name, value in os.environ.items():
            if name.startswith(prefix):
                field = name[len(prefix):].lower()
                if field not in cls.model_fields:
                    raise ValueError("Unknown service configuration variable.")
                values[field] = value
        values.update({name: value for name, value in overrides.items() if value is not None})
        return cls(**values)


class ServerSettings(EnvironmentSettings):
    host: str = Field(default="127.0.0.1", min_length=1, max_length=253, pattern=r"^[A-Za-z0-9.:-]+$")
    port: int = Field(default=8042, ge=1, le=65535)


class AppSettings(EnvironmentSettings):
    readiness_timeout_seconds: float = Field(default=2, gt=0, le=30)


def read_config(path):
    with Path(path).open("rb") as source:
        payload = source.read(CONFIG_LIMIT + 1)
    if len(payload) > CONFIG_LIMIT:
        raise ValueError("Configuration exceeds 64 KiB.")

    def unique_fields(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("Configuration contains a duplicate field.")
            result[name] = value
        return result

    value = json.loads(payload, object_pairs_hook=unique_fields)
    if not isinstance(value, dict):
        raise ValueError("Configuration must be a JSON object.")
    return value
