"""Allowlisted operational fields; no request payloads or exception text."""

import json
import logging
import re


class OperationalFormatter(logging.Formatter):
    def format(self, record):
        fields = {"level": record.levelname}
        for name in ("event", "service", "code", "correlation_id"):
            value = getattr(record, name, None)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
                fields[name] = value
        return json.dumps(fields, sort_keys=True)


def configure_logging():
    logger = logging.getLogger("susi_brain")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(OperationalFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
