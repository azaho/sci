"""PNG profile checks for this corpus, not a complete PNG conformance validator."""
import io
import struct
import zlib

from PIL import Image


def baseline_png(path, data):
    if not path.lower().endswith('.png') or len(data) > 2097152:
        return False
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        return False
    try:
        chunks = []
        offset = 8
        while offset < len(data):
            size, kind = struct.unpack_from('>I4s', data, offset)
            end = offset + 12 + size
            if end > len(data) or not all(65 <= c <= 90 or 97 <= c <= 122 for c in kind):
                return False
            if kind[2] & 32 or kind in (b'acTL', b'fcTL', b'fdAT'):
                return False
            if not kind[0] & 32 and kind not in (b'IHDR', b'PLTE', b'IDAT', b'IEND'):
                return False
            body = data[offset + 8:end - 4]
            crc = struct.unpack_from('>I', data, end - 4)[0]
            if crc != zlib.crc32(kind + body):
                return False
            chunks.append((kind, body))
            offset = end
        kinds = [kind for kind, _ in chunks]
        if not kinds or kinds[0] != b'IHDR' or kinds[-1] != b'IEND':
            return False
        if kinds.count(b'IHDR') != 1 or kinds.count(b'IEND') != 1 or chunks[-1][1]:
            return False
        width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', chunks[0][1])
        if not (1 <= width <= 8192 and 1 <= height <= 8192 and width * height <= 8000000):
            return False
        depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
        if depth not in depths.get(color, ()) or compression or filtering or interlace not in (0, 1):
            return False
        if b'IDAT' not in kinds or kinds.count(b'PLTE') > 1:
            return False
        first, last = kinds.index(b'IDAT'), len(kinds) - 1 - kinds[::-1].index(b'IDAT')
        if any(kind != b'IDAT' for kind in kinds[first:last + 1]):
            return False
        if b'PLTE' in kinds:
            index = kinds.index(b'PLTE')
            size = len(chunks[index][1])
            if index > first or color in (0, 4) or not (0 < size <= 768) or size % 3:
                return False
            if color == 3 and size // 3 > 2 ** depth:
                return False
        elif color == 3:
            return False

        # Bound decompression by the exact scanline lengths, including Adam7 passes.
        passes = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
                  (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)) if interlace else ((0, 0, 1, 1),)
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
        rows = []
        for x, y, dx, dy in passes:
            w, h = max(0, (width - x + dx - 1) // dx), max(0, (height - y + dy - 1) // dy)
            if w and h:
                rows.extend([(w * channels * depth + 7) // 8 + 1] * h)
        limit = sum(rows)
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(b''.join(body for kind, body in chunks if kind == b'IDAT'), limit + 1)
        if len(pixels) != limit or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            return False
        offset = 0
        for size in rows:
            if pixels[offset] > 4:
                return False
            offset += size
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
        return True
    except (ValueError, OSError, SyntaxError, struct.error, zlib.error):
        return False
