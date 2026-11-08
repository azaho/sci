"""Ed25519 signatures with the protocol's additional point checks (§5.1).

Verification follows RFC 8032 §5.1.7 and additionally requires that the public
key ``A`` and the signature point ``R`` are canonical encodings of nonidentity
points in the prime-order subgroup. With both points in that subgroup the
cofactored and cofactorless equations agree, so the final equation is checked
by ``cryptography`` (OpenSSL), which also rejects ``S >= L``; the scalar range
is checked here as well so that the failure is reported precisely.

Signing uses ``cryptography``, which implements the deterministic procedure
of RFC 8032 §5.1.6.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .errors import Invalid, Issue

__all__ = [
    "GROUP_ORDER",
    "SIGNATURE_LENGTH",
    "check_public_key_point",
    "sign",
    "verify",
]

_P: Final = 2**255 - 19
GROUP_ORDER: Final = 2**252 + 27742317777372353535851937790883648493
"""The prime order L of the Ed25519 base-point subgroup."""
_D: Final = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1: Final = pow(2, (_P - 1) // 4, _P)
SIGNATURE_LENGTH: Final = 64

_Point = tuple[int, int, int, int]  # extended twisted Edwards coordinates
_IDENTITY: Final[_Point] = (0, 1, 1, 0)


def _decode_point(encoded: bytes) -> _Point | None:
    """RFC 8032 §5.1.3 decoding; None for a noncanonical or off-curve encoding."""
    if len(encoded) != 32:
        return None
    value = int.from_bytes(encoded, "little")
    sign = value >> 255
    y = value & ((1 << 255) - 1)
    if y >= _P:
        return None
    u = (y * y - 1) % _P
    v = (_D * y * y + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if x == 0 and sign == 1:
        return None
    if (x & 1) != sign:
        x = _P - x
    return (x, y, 1, x * y % _P)


def _add(a: _Point, b: _Point) -> _Point:
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    k_a = (y1 - x1) * (y2 - x2) % _P
    k_b = (y1 + x1) * (y2 + x2) % _P
    k_c = 2 * t1 * t2 * _D % _P
    k_d = 2 * z1 * z2 % _P
    e, f, g, h = k_b - k_a, k_d - k_c, k_d + k_c, k_b + k_a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _multiply(scalar: int, point: _Point) -> _Point:
    result = _IDENTITY
    addend = point
    while scalar:
        if scalar & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        scalar >>= 1
    return result


def _is_identity(point: _Point) -> bool:
    x, y, z, _ = point
    return x % _P == 0 and (y - z) % _P == 0


@lru_cache(maxsize=4096)
def _point_problem(encoded: bytes) -> str | None:
    """Return why ``encoded`` is not an admissible point, or None."""
    point = _decode_point(encoded)
    if point is None:
        return "noncanonical or invalid point encoding"
    if _is_identity(point):
        return "identity point"
    if not _is_identity(_multiply(GROUP_ORDER, point)):
        return "point outside the prime-order subgroup"
    return None


def check_public_key_point(public_key: bytes, *, file: str | None = None) -> None:
    """Require ``A`` to be a canonical nonidentity prime-order point (§5.1)."""
    problem = _point_problem(bytes(public_key))
    if problem is not None:
        raise Invalid(
            Issue(
                "public_key_point",
                "public key A must be a nonidentity member of the prime-order subgroup",
                section="5.1",
                file=file,
                field="/public_key/x",
                observed=problem,
            )
        )


def verify(public_key: bytes, signature: bytes, message: bytes, *, file: str | None = None) -> None:
    """Verify a pure Ed25519 signature under §5.1. Raises :class:`Invalid`."""

    def fail(code: str, requirement: str, observed: str | None = None) -> Invalid:
        return Invalid(Issue(code, requirement, section="5.1", file=file, observed=observed))

    if len(signature) != SIGNATURE_LENGTH:
        raise fail(
            "signature_length",
            "signature must be exactly the raw 64 Ed25519 signature bytes",
            f"{len(signature)} bytes",
        )
    check_public_key_point(public_key, file=file)
    problem = _point_problem(bytes(signature[:32]))
    if problem is not None:
        raise fail(
            "signature_r_point",
            "signature point R must be a nonidentity member of the prime-order subgroup",
            problem,
        )
    if int.from_bytes(signature[32:], "little") >= GROUP_ORDER:
        raise fail("signature_scalar", "signature scalar S must be less than L (RFC 8032 §5.1.7)")
    try:
        Ed25519PublicKey.from_public_bytes(bytes(public_key)).verify(bytes(signature), message)
    except (InvalidSignature, ValueError):
        raise fail("signature_mismatch", "Ed25519 signature does not verify") from None


def sign(private_key: Ed25519PrivateKey, message: bytes) -> bytes:
    """Deterministic RFC 8032 §5.1.6 signature (64 raw bytes)."""
    return private_key.sign(message)
