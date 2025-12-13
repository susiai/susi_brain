"""First/terminal SSE routing annotations following triton_llmlb's wire format.

Middle lines remain bytes. Only the first data payload and potential terminal
payloads are parsed, following the sibling's stream_passthrough.py strategy.
"""

import re
from json import dumps, loads

LINE_END = re.compile(rb'\r\n|\r|\n')
TERMINAL = re.compile(rb'(?<!\\)"finish_reason"\s*:\s*"')
BLANK_LINES = re.compile(rb'(?:\r\n|\r|\n)*')


def data_payload(line):
    stripped = line.strip()
    return stripped[5:].strip() if stripped.startswith(b'data:') else None


def is_terminal(payload):
    choices = payload.get('choices') if isinstance(payload, dict) else None
    return isinstance(choices, list) and any(isinstance(choice, dict)
        and choice.get('finish_reason') is not None for choice in choices)


class StreamMetadata:
    """Assemble byte lines across network chunks without decoding middle events."""

    def __init__(self, metadata):
        self.metadata = dict(metadata)
        self.first_data_seen = self.terminal_annotated = self.done = False
        self.pending, self.pending_size, self.pending_cr = [], 0, False

    def _line(self, line):
        payload = data_payload(line)
        if payload is None:
            return line
        if payload == b'[DONE]':
            self.done = True
            return line
        first = not self.first_data_seen
        if first:
            self.first_data_seen = True
        if not first and (self.terminal_annotated or not TERMINAL.search(payload)):
            return line
        try:
            value = loads(payload)
            if not isinstance(value, dict):
                return line
            terminal = is_terminal(value)
            if not first and not terminal:
                return line
            value.update(self.metadata)
            result = b'data: ' + dumps(value, ensure_ascii=False).encode('utf-8') + b'\n\n'
        except (ValueError, UnicodeError, RecursionError):
            return line
        self.terminal_annotated = self.terminal_annotated or terminal
        return result

    def _take(self, suffix=b''):
        if self.pending:
            self.pending.append(suffix)
            line = b''.join(self.pending)
        else:
            line = suffix
        self.pending, self.pending_size, self.pending_cr = [], 0, False
        return line

    def _finish_done(self, remaining):
        # Retain already received blank framing, without waiting on an open socket.
        blank = BLANK_LINES.match(remaining).end()
        if blank:
            yield remaining[:blank]
        self.pending, self.pending_size, self.pending_cr = [], 0, False

    def feed(self, chunk, *, final=False):
        if self.done:
            return
        start = 0
        if self.pending_cr and (chunk or final):
            start = 1 if chunk.startswith(b'\n') else 0
            yield self._line(self._take(chunk[:start]))
            if self.done:
                yield from self._finish_done(chunk[start:])
                return
        for match in LINE_END.finditer(chunk, start):
            end = match.end()
            if end == len(chunk) and chunk[end - 1:] == b'\r' and not final:
                break  # A CRLF separator can straddle two network chunks.
            yield self._line(self._take(chunk[start:end]))
            start = end
            if self.done:
                yield from self._finish_done(chunk[start:])
                return
        if start < len(chunk):
            part = chunk[start:]
            self.pending.append(part)
            self.pending_size += len(part)
            self.pending_cr = chunk.endswith(b'\r')
        if final and self.pending:
            yield self._line(self._take())
        elif self.pending_size <= 64 and self.pending:
            # Some servers omit [DONE]'s newline and keep the connection open.
            pending = b''.join(self.pending)
            if data_payload(pending) == b'[DONE]':
                yield self._line(self._take())
