"""Configuration for Compute's chat proxy and existing audio/file services."""

import ipaddress
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, SecretStr, field_validator, model_validator
from susi_brain.shared.config.settings import AppSettings


def default_data_path():
    source = Path(__file__).resolve()
    root = source.parents[3]
    return root / "data" if source == root / "src/susi_brain/compute/config.py" else Path("data")


class ChatEndpoint(BaseModel):
    """An operator-configured API base URL and its own optional Bearer key."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    url: str = Field(min_length=1, max_length=2048)
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), exclude=True, repr=False)
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    _resolved_api_key: SecretStr = PrivateAttr(default_factory=lambda: SecretStr(""))

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value):
        try:
            if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
                raise ValueError()
            parsed = urlsplit(value)
            host, port = parsed.hostname, parsed.port
            if (parsed.scheme not in {"http", "https"} or not host
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment or "?" in value or "#" in value
                    or port is not None and not 1 <= port <= 65535):
                raise ValueError()
            if ":" in host:
                host = "[" + ipaddress.IPv6Address(host).compressed + "]"
            elif len(host) > 253 or not all(re.fullmatch(
                    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part) for part in host.split(".")):
                raise ValueError()
            path = parsed.path.rstrip("/")
            if "%" in path or "//" in path or any(part in {".", ".."} for part in path.split("/")):
                raise ValueError()
            if path.endswith("/chat/completions"):
                path = path[:-len("/chat/completions")]
            default_port = 443 if parsed.scheme == "https" else 80
            authority = host.lower() + (f":{port}" if port is not None and port != default_port else "")
            return f"{parsed.scheme}://{authority}{path}"
        except ValueError:
            raise ValueError("Use an HTTP(S) API base URL without embedded credentials, queries or fragments.") from None

    @model_validator(mode="after")
    def resolve_api_key(self):
        value = self.api_key.get_secret_value()
        if self.api_key_env is not None:
            if value:
                raise ValueError("Choose api_key or api_key_env, not both.")
            value = os.getenv(self.api_key_env, "")
            if not value:
                raise ValueError("The endpoint API key environment variable must be set and nonempty.")
        if len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError("API keys must be bounded printable ASCII tokens without whitespace.")
        self._resolved_api_key = SecretStr(value)
        return self

    def authorization_headers(self):
        key = self._resolved_api_key.get_secret_value()
        return {"Authorization": "Bearer " + key} if key else {}


class ChatModel(BaseModel):
    """A concrete model name and an operator-assigned quality ranking."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=256)
    ranking: float = Field(default=0, strict=True, allow_inf_nan=False)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Model names must not contain whitespace or control characters.")
        return value


def speech_model_key(name):
    """Ollama treats a missing tag as :latest."""
    return name.casefold() + (':latest' if ':' not in name.rsplit('/', 1)[-1] else '')


class SpeechModel(BaseModel):
    """An explicitly allowed Ollama speech model and its default voice."""

    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=256)
    voice: str = Field(default='default', min_length=1, max_length=256)

    @field_validator('name')
    @classmethod
    def validate_name(cls, value):
        return ChatModel.validate_name(value)

    @field_validator('voice')
    @classmethod
    def validate_voice(cls, value):
        if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('Voice names must be nonempty and contain no control characters.')
        return value


class ComputeSettings(AppSettings):
    openai_api_key: SecretStr = SecretStr("")
    susi_api_key: SecretStr = SecretStr("")
    data_path: Path = Field(default_factory=default_data_path)
    endpoints: list[str | ChatEndpoint] = Field(default_factory=list, max_length=32)
    smart: list[str | ChatModel] = Field(default_factory=list, max_length=128)
    small: list[str | ChatModel] = Field(default_factory=list, max_length=128)
    speech: list[str | SpeechModel] = Field(default_factory=list, max_length=128)
    model_probe_timeout_seconds: float = Field(default=3, gt=0, le=30)
    chat_timeout_seconds: float = Field(default=120, gt=0, le=600)
    speech_timeout_seconds: float = Field(default=120, gt=0, le=600)
    calibration_timeout_seconds: float = Field(default=60, gt=0, le=600)
    calibration_max_pairs: int = Field(default=1024, ge=1, le=16384)
    request_log_capacity: int = Field(default=10000, ge=1, le=100000)
    request_log_directory: Path = Path('logs/compute')

    @model_validator(mode='after')
    def private_log_directory(self):
        if self.request_log_directory.resolve().is_relative_to(self.data_path.resolve()):
            raise ValueError('The request log directory must be outside the public file-service data path.')
        speech = {speech_model_key(model.name) for model in self.speech_models}
        if speech.intersection(speech_model_key(model.name) for group in ('smart', 'small') for model in self.chat_models(group)):
            raise ValueError('Speech models must be separate from the smart/small chat groups.')
        return self

    @field_validator("endpoints", "smart", "small", "speech", mode="before")
    @classmethod
    def parse_lists(cls, value):
        if isinstance(value, str):
            if len(value.encode()) > 64 * 1024:
                raise ValueError("Chat configuration lists must fit within 64 KiB.")
            return json.loads(value)
        return value

    @field_validator("endpoints")
    @classmethod
    def validate_endpoints(cls, values):
        normalized = []
        for value in values:
            if isinstance(value, ChatEndpoint):
                normalized.append(value)
                continue
            try:
                if len(value) > 260 or any(c.isspace() or ord(c) < 32 for c in value):
                    raise ValueError()
                parsed = urlsplit("//" + value)
                host, port = parsed.hostname, parsed.port
                if (not host or port is None or not 1 <= port <= 65535
                        or parsed.username is not None or parsed.password is not None
                        or parsed.path or parsed.query or parsed.fragment
                        or "?" in value or "#" in value):
                    raise ValueError()
                if ":" in host:
                    ipaddress.IPv6Address(host)
                    host = "[" + host + "]"
                elif not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                             for part in host.split(".")):
                    raise ValueError()
                endpoint = f"{host.lower()}:{port}"
                normalized.append(endpoint)
            except ValueError:
                raise ValueError("Use unique host:port endpoints, without schemes, credentials or paths.") from None
        urls = [cls.endpoint_config(value).url for value in normalized]
        if len(set(urls)) != len(urls):
            raise ValueError("Chat endpoint API base URLs must be unique.")
        return normalized

    @staticmethod
    def endpoint_config(value):
        return ChatEndpoint(url=f"http://{value}/v1") if isinstance(value, str) else value

    @property
    def chat_endpoints(self):
        return tuple(self.endpoint_config(value) for value in self.endpoints)

    @field_validator("smart", "small")
    @classmethod
    def validate_models(cls, values):
        names = [cls.configured_model(value).name for value in values]
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("Use unique, nonempty model names without whitespace or control characters.")
        return values

    @staticmethod
    def configured_model(value):
        return ChatModel(name=value) if isinstance(value, str) else value

    def chat_models(self, group):
        return tuple(self.configured_model(value) for value in getattr(self, group))

    @field_validator('speech')
    @classmethod
    def validate_speech_models(cls, values):
        names = [SpeechModel(name=value).name if isinstance(value, str) else value.name for value in values]
        if len({speech_model_key(name) for name in names}) != len(names):
            raise ValueError('Use unique speech model names.')
        return values

    @property
    def speech_models(self):
        return tuple(SpeechModel(name=value) if isinstance(value, str) else value for value in self.speech)

    @classmethod
    def from_env(cls, **overrides):
        base = dict(overrides.pop("base", None) or {})
        for environment, field in (("OPENAI_API_KEY", "openai_api_key"),
                                   ("SUSI_API_KEY", "susi_api_key")):
            if environment in os.environ:
                base[field] = os.environ[environment]
        return super().from_env("SUSI_COMPUTE_", base=base, **overrides)
