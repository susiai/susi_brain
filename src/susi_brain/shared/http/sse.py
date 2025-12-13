"""Incremental UTF-8/SSE framing adapted from Project Euler LLM Benchmark.

Modified for SUSI Brain: strict decoding, bounded frames and multiline events.
SPDX-License-Identifier: Apache-2.0
"""

import codecs
import json


def _terminal(value):
    # A complete JSON choice can end at an arbitrary TCP byte boundary before
    # its newline. Only [DONE] unambiguously ends a live SSE connection here.
    return value.strip() == "[DONE]"


class SSEDecoder:
    def __init__(self, max_frame_bytes=1024 * 1024):
        self.max_frame_bytes = max_frame_bytes
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._pending = ""
        self._data = []
        self.unterminated_terminal = False
        self._skip_lf = False

    def _newlines(self, text):
        normalized = []
        for character in text:
            if self._skip_lf:
                self._skip_lf = False
                if character == "\n":
                    continue
            if character == "\r":
                normalized.append("\n")
                self._skip_lf = True
            else:
                normalized.append(character)
        return "".join(normalized)

    def _line(self, line):
        line = line.rstrip("\r").lstrip("\ufeff")
        if not line:
            if self._data:
                payload, self._data = "\n".join(self._data), []
                return payload
            return None
        field, separator, value = line.partition(":")
        if not separator or field != "data":
            return None
        if value.startswith(" "):
            value = value[1:]
        self._data.append(value)
        payload = "\n".join(self._data)
        if len(payload.encode("utf-8")) > self.max_frame_bytes:
            raise ValueError("SSE frame exceeds its size limit.")
        # Accept single-line compatible servers without a blank separator, but
        # keep incomplete JSON data lines for a standards-style multiline event.
        try:
            json.loads(payload)
            complete = True
        except ValueError:
            complete = payload.strip() == "[DONE]"
        if complete:
            self._data = []
            return payload
        return None

    def feed(self, chunk, *, final=False):
        self.unterminated_terminal = False
        self._pending += self._newlines(self._decoder.decode(chunk, final=final))
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            payload = self._line(line)
            if payload is not None:
                yield payload
        if len(self._pending.encode("utf-8")) > self.max_frame_bytes:
            raise ValueError("SSE frame exceeds its size limit.")
        # Some servers omit the final newline and keep their connection open.
        field, separator, value = self._pending.partition(":")
        if final or (separator and field.lstrip("\ufeff") == "data" and _terminal(value)):
            self.unterminated_terminal = not final
            payload = self._line(self._pending)
            self._pending = ""
            if payload is not None:
                yield payload
            if final:
                payload = self._line("")
                if payload is not None:
                    yield payload
