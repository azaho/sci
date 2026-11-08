"""SCI baseline display structure checks; no CRC checks or image decoding."""
import struct


def baseline_png(path, data):
    if not path.lower().endswith('.png') or len(data) > 2097152:
        return False
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        return False
    offset = 8
    while offset < len(data):
        if len(data) - offset < 12:
            return False
        size, kind = struct.unpack_from('>I4s', data, offset)
        end = offset + 12 + size
        if end > len(data) or kind in (b'acTL', b'fcTL', b'fdAT'):
            return False
        if offset == 8:
            if kind != b'IHDR' or size != 13:
                return False
            width, height = struct.unpack_from('>II', data, offset + 8)
            if not (1 <= width <= 8192 and 1 <= height <= 8192 and width * height <= 8000000):
                return False
        elif kind == b'IHDR':
            return False
        if kind == b'IEND':
            return size == 0 and end == len(data)
        offset = end
    return False
