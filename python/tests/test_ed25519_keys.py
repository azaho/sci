from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pysci import ed25519
from pysci.errors import Invalid, IssueCollector
from pysci.keys import (
    PublicKey,
    load_private_key_pem,
    load_public_key_pem,
    looks_like_private_key,
    parse_public_jwk,
    private_key_to_pem,
    public_key_of,
    x_to_bytes,
)

L = ed25519.GROUP_ORDER
# Encodings of small-order points (from the Ed25519 low-order point list).
IDENTITY = (1).to_bytes(32, "little")
ORDER_TWO = (2**255 - 20).to_bytes(32, "little")  # (0, -1)
ORDER_FOUR = bytes(32)  # (sqrt(-1), 0)


def code_of(public: bytes, signature: bytes, message: bytes = b"m") -> str:
    with pytest.raises(Invalid) as info:
        ed25519.verify(public, signature, message)
    return info.value.issue.code


def test_sign_verify_roundtrip_is_deterministic(private_key: Ed25519PrivateKey) -> None:
    public = public_key_of(private_key)
    first = ed25519.sign(private_key, b"message")
    assert first == ed25519.sign(private_key, b"message")
    ed25519.verify(public.raw, first, b"message")
    assert code_of(public.raw, first, b"other") == "signature_mismatch"


def test_rejects_length_scalar_and_points(private_key: Ed25519PrivateKey) -> None:
    public = public_key_of(private_key).raw
    signature = ed25519.sign(private_key, b"m")
    assert code_of(public, signature[:63]) == "signature_length"
    assert code_of(public, signature + b"\0") == "signature_length"
    s = int.from_bytes(signature[32:], "little") + L
    assert code_of(public, signature[:32] + s.to_bytes(32, "little")) == "signature_scalar"
    for bad in (IDENTITY, ORDER_TWO, ORDER_FOUR):
        assert code_of(bad, signature) == "public_key_point"
        assert code_of(public, bad + signature[32:]) == "signature_r_point"
    noncanonical_y = (2**255 - 19 + 1).to_bytes(32, "little")  # y = p + 1
    assert code_of(noncanonical_y, signature) == "public_key_point"


def test_mixed_order_key_is_rejected() -> None:
    # A + T where T has order 2: still on the curve but outside the subgroup.
    base_key = Ed25519PrivateKey.from_private_bytes(b"\x07" * 32)
    raw = public_key_of(base_key).raw
    point = ed25519._decode_point(raw)
    torsion = ed25519._decode_point(ORDER_TWO)
    assert point is not None and torsion is not None
    mixed = ed25519._add(point, torsion)
    x, y, z, _ = mixed
    p = 2**255 - 19
    zinv = pow(z, p - 2, p)
    ax, ay = x * zinv % p, y * zinv % p
    encoded = (ay | ((ax & 1) << 255)).to_bytes(32, "little")
    with pytest.raises(Invalid):
        ed25519.check_public_key_point(encoded)


def test_jwk_roundtrip_and_canonical_x(private_key: Ed25519PrivateKey) -> None:
    public = public_key_of(private_key)
    assert parse_public_jwk(public.to_jwk()) == public
    assert PublicKey.from_x(public.x) == public
    assert x_to_bytes(public.x + "=") is None
    # Flip an unused low bit of the final character.
    last = public.x[-1]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    tweaked = public.x[:-1] + alphabet[alphabet.index(last) ^ 1]
    assert x_to_bytes(tweaked) is None


@pytest.mark.parametrize(
    ("jwk", "code"),
    [
        ([], "jwk_type"),
        ({"kty": "OKP", "crv": "Ed25519"}, "jwk_fields"),
        ({"kty": "OKP", "crv": "X25519", "x": "A" * 43}, "jwk_crv"),
        ({"kty": "EC", "crv": "Ed25519", "x": "A" * 43}, "jwk_kty"),
        ({"kty": "OKP", "crv": "Ed25519", "x": "A" * 43, "d": "x"}, "jwk_fields"),
        ({"kty": "OKP", "crv": "Ed25519", "x": 5}, "jwk_x_encoding"),
    ],
)
def test_jwk_rejections(jwk: object, code: str) -> None:
    collector = IssueCollector()
    assert parse_public_jwk(jwk, collector=collector) is None
    assert collector.issues[0].code == code


def test_pem_roundtrip(private_key: Ed25519PrivateKey) -> None:
    pem = private_key_to_pem(private_key)
    assert looks_like_private_key(pem)
    loaded = load_private_key_pem(pem)
    assert public_key_of(loaded) == public_key_of(private_key)
    public = public_key_of(private_key)
    assert load_public_key_pem(public.to_pem()) == public
    assert not looks_like_private_key(public.to_pem())
    encrypted = private_key_to_pem(private_key, b"secret")
    assert public_key_of(load_private_key_pem(encrypted, b"secret")) == public


def test_private_jwk_detection() -> None:
    assert looks_like_private_key(b'{"kty":"OKP","crv":"Ed25519","d":"' + b"A" * 43 + b'"}')
    assert not looks_like_private_key(b"plain text with kty mention")
