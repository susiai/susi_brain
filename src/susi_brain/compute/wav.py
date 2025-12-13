"""Bounded parsing of Ollama's PCM WAV headers, including unknown stream sizes."""

import struct

MAX_WAV_HEADER = 64 * 1024
UNKNOWN_SIZE = 0xffffffff


def wav_header(data):
    """Return format/data metadata once complete, or None for an incomplete header."""
    if len(data) < 12:
        return None
    if data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        raise ValueError('Expected a RIFF WAV response.')
    offset, audio = 12, None
    while offset + 8 <= len(data):
        kind = data[offset:offset + 4]
        size = struct.unpack_from('<I', data, offset + 4)[0]
        offset += 8
        if kind == b'data':
            if audio is None:
                raise ValueError('WAV data precedes its format.')
            if size == 0 or size != UNKNOWN_SIZE and size % audio['bytes_per_frame']:
                raise ValueError('Empty or unaligned WAV data.')
            return {**audio, 'offset': offset, 'data_size': size}
        end = offset + size + size % 2
        if end > MAX_WAV_HEADER:
            raise ValueError('WAV header exceeds its size limit.')
        if end > len(data):
            return None
        if kind == b'fmt ':
            if size < 16:
                raise ValueError('Invalid WAV format.')
            code, channels, rate, byte_rate, alignment, bits = struct.unpack_from('<HHIIHH', data, offset)
            if (code, channels, rate, alignment, bits, byte_rate) != (1, 1, 24000, 2, 16, 48000):
                raise ValueError('Expected 24 kHz mono 16-bit PCM WAV.')
            audio = {'sample_rate': rate, 'channels': channels, 'sample_width': bits // 8,
                     'bytes_per_frame': alignment, 'bytes_per_second': byte_rate}
        offset = end
    if offset + 8 > MAX_WAV_HEADER:
        raise ValueError('WAV header exceeds its size limit.')
    return None


def wav_duration(info, total_bytes):
    """Validate the complete audio length and return duration from actual PCM frames."""
    available = total_bytes - info['offset']
    size = available if info['data_size'] == UNKNOWN_SIZE else info['data_size']
    if size <= 0 or size > available or size % info['bytes_per_frame']:
        raise ValueError('Empty, truncated or unaligned WAV audio.')
    return size / info['bytes_per_second']
