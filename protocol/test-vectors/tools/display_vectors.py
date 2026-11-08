"""Deterministic PNG display fixtures. All publication signatures remain valid."""
import struct
import zlib


SIGNATURE = b'\x89PNG\r\n\x1a\n'


def chunk(kind, data=b''):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))


def png(width=1, height=1, depth=8, color=0, interlace=0):
    header = chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, depth, color, 0, 0, interlace))
    passes = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
              (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)) if interlace else ((0, 0, 1, 1),)
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    pixels = bytearray()
    for x, y, dx, dy in passes:
        w, h = max(0, (width - x + dx - 1) // dx), max(0, (height - y + dy - 1) // dy)
        if w and h:
            pixels.extend(bytes(((w * depth * channels + 7) // 8 + 1) * h))
    palette = chunk(b'PLTE', b'\0\0\0\xff\xff\xff') if color == 3 else b''
    return SIGNATURE + header + palette + chunk(b'IDAT', zlib.compress(bytes(pixels), 9)) + chunk(b'IEND')


def generate_display_vectors(pub, emit, bundle):
    meta = {'title': 'PNG display profile test', 'abstract': 'Synthetic image; no research claim.'}
    image = png()

    def case(name, data, baseline, description, path='files/figure.png'):
        pub('display-png-' + name, '2.1', description, baseline=baseline,
            files={path: data}, metadata={**meta, 'display': [{'path': path}]})

    case('static', image, True, 'A complete static PNG meets the display profile.')
    case('upper-extension', image, True, 'Compare the extension without ASCII case.', 'files/figure.PNG')
    case('mixed-extension', image, True, 'Mixed-case extension; exact manifest path still required.', 'files/figure.PnG')
    for color, depths in ((0, (1, 2, 4, 8, 16)), (2, (8, 16)), (3, (1, 2, 4, 8)), (4, (8, 16)), (6, (8, 16))):
        for depth in depths:
            case(f'color-{color}-depth-{depth}', png(3, 2, depth, color), True,
                 'PNG color types and bit depths remain available within the profile limits.')
    case('interlaced', png(9, 9, interlace=1), True, 'Static Adam7 PNG is permitted.')
    case('pixels-limit', png(4000, 2000), True, 'Exactly 8,000,000 pixels.')
    case('pixels-over', png(4001, 2000), False, 'Pixel count exceeds the limit; each dimension is within its limit.')
    for axis in ('width', 'height'):
        for size in (8192, 8193):
            case(f'{axis}-{size}', png(**{axis: size}), size == 8192,
                 f'{axis.capitalize()} at or one pixel above the limit; pixel count is within its limit.')
    for size in (2097152, 2097153):
        padded = image[:-12] + chunk(b'npAD', bytes(size - len(image) - 12)) + image[-12:]
        assert len(padded) == size
        case(f'bytes-{size}', padded, size == 2097152,
             'Whole file size at or one byte above 2 MiB; valid private ancillary chunk supplies padding.')

    header, compressed, end = image[8:33], image[33:-12], image[-12:]
    control = struct.pack('>IIIIIHHBB', 0, 1, 1, 0, 0, 1, 100, 0, 0)
    apng = SIGNATURE + header + chunk(b'acTL', struct.pack('>II', 1, 0)) + chunk(b'fcTL', control) + compressed + end
    case('apng-single-frame', apng, False, 'Valid single-frame APNG is outside the static profile.')
    second = chunk(b'fcTL', struct.pack('>IIIIIHHBB', 1, 1, 1, 0, 0, 1, 100, 0, 0))
    apng2 = (SIGNATURE + header + chunk(b'acTL', struct.pack('>II', 2, 0)) + chunk(b'fcTL', control)
             + compressed + second + chunk(b'fdAT', struct.pack('>I', 2) + zlib.compress(b'\0\0', 9)) + end)
    case('apng-two-frames', apng2, False, 'Valid two-frame APNG is outside the static profile.')
    for kind, body in ((b'acTL', struct.pack('>II', 1, 0)), (b'fcTL', control), (b'fdAT', b'\0' * 4)):
        case('chunk-' + kind.decode(), SIGNATURE + header + chunk(kind, body) + compressed + end,
             False, 'Each forbidden animation chunk excludes the file, even without other animation chunks.')

    invalid = {
        'empty': b'', 'signature-only': SIGNATURE, 'svg-disguised': b'<svg xmlns="http://www.w3.org/2000/svg"/>',
        'bad-crc': image[:-1] + bytes([image[-1] ^ 1]),
        'truncated-chunk': image[:-3], 'trailing-bytes': image + b'x',
        'duplicate-header': SIGNATURE + header + header + compressed + end,
        'missing-idat': SIGNATURE + header + end, 'missing-iend': image[:-12],
        'unknown-critical': SIGNATURE + header + chunk(b'ABCD') + compressed + end,
        'bad-zlib': SIGNATURE + header + chunk(b'IDAT', b'not zlib') + end,
        'short-pixels': SIGNATURE + header + chunk(b'IDAT', zlib.compress(b'\0')) + end,
        'extra-pixels': SIGNATURE + header + chunk(b'IDAT', zlib.compress(b'\0\0\0')) + end,
        'bad-filter': SIGNATURE + header + chunk(b'IDAT', zlib.compress(b'\5\0')) + end,
        'extra-zlib': SIGNATURE + header + compressed + chunk(b'IDAT', zlib.compress(b'\0\0')) + end,
        'zero-width': png(0, 1), 'zero-height': png(1, 0),
    }
    for name, data in invalid.items():
        case(name, data, False, 'Signed file bytes are intact, but the file is not a valid static PNG; publication stays valid.')
    case('wrong-extension', image, False, 'PNG contents require the .png extension for this profile.', 'files/figure.txt')
    pub('display-png-empty-array', '2.1', 'An empty display array meets the profile.', metadata={**meta, 'display': []})
    pub('display-png-supporting-files', '2.1', 'Unselected SVG and malformed PNG files stay opaque supporting files.',
        files={'files/source.svg': invalid['svg-disguised'], 'files/broken.png': b'not PNG', 'files/figure.png': image},
        metadata={**meta, 'display': [{'path': 'files/figure.png'}]})
    for count in (3, 4):
        files = {f'files/{i}.png': image for i in range(count)}
        pub(f'display-png-count-{count}', '2.1', 'Valid PNGs isolate the existing display-count limit.',
            baseline=count == 3, files=files, metadata={**meta, 'display': [{'path': path} for path in files]})

    # Exercise the same profile on a loose bundle without changing the signed input.
    import io
    import zipfile
    for name, data, baseline in (('static', image, True), ('bad-crc', invalid['bad-crc'], False)):
        z, _, _ = bundle(files={'files/figure.png': data}, metadata={**meta, 'display': [{'path': 'files/figure.png'}]})
        with zipfile.ZipFile(io.BytesIO(z)) as archive:
            files = {path: archive.read(path) for path in archive.namelist()}
        emit('display-png-' + name + '-loose', 'publication', '2.1', 'Loose-file form of the PNG profile case.',
             files, {'outcome': 'valid', 'baseline': baseline})
