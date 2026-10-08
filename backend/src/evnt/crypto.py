"""
Hybrid public-key decryption for the encrypted ingest endpoint.

Clients (Android / iOS / Web) seal a Snowplow payload with the collector's
public key; only the collector holds the matching private key. The scheme is a
sealed box: an ephemeral X25519 key agreement feeds HKDF-SHA256, whose output
keys AES-256-GCM.

Why this combination:

- **X25519** is native on every target (iOS CryptoKit, Android via Tink or
  Conscrypt, Web via WebCrypto or ``@noble/curves``), has fixed 32-byte keys,
  and needs neither ASN.1 parsing nor curve-point validation.
- **AES-256-GCM** is the only AEAD WebCrypto exposes natively, and it is
  hardware-accelerated on current phones.
- **A fresh ephemeral sender key per event** means a per-event content key, so
  a nonce can never be reused across payloads. A static client key would buy no
  authentication anyway, since it would ship inside the app bundle.

The envelope is self-describing, so several key pairs stay live at once and
rotation needs no client flag day: the key id travels in cleartext.

Wire format (little of it is negotiable; clients must match byte for byte)::

    offset  size  field
    0       4     magic      b"EVN1"
    4       1     version    0x01
    5       1     flags      bit0 = plaintext deflated before sealing
                             (gzip or zlib container, auto-detected)
    6       1     kid_len    1..32
    7       N     kid        ASCII key id
    7+N     32    epk        ephemeral X25519 public key
    39+N    12    nonce      AES-GCM nonce
    51+N    ..    ct         AES-256-GCM ciphertext || 16-byte tag

Key schedule::

    shared = X25519(ephemeral_secret, recipient_public)
    key    = HKDF-SHA256(ikm=shared, salt=b"", info=HKDF_INFO || epk || rpk, 32)
    aad    = envelope[0 : 39+N]          # magic .. epk inclusive

Binding ``epk`` and the recipient public key into ``info`` is the standard
ECIES anti-confusion measure, and using the header as AAD means a tampered
version, flag, or key id fails the tag check instead of silently changing how
the plaintext is read.
"""

from __future__ import annotations

import base64
import binascii
import os
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

from evnt.constants import (
    AES_GCM_NONCE_SIZE,
    AES_GCM_TAG_SIZE,
    ENVELOPE_FLAG_GZIP,
    ENVELOPE_MAGIC,
    ENVELOPE_MAX_KID_LEN,
    ENVELOPE_VERSION,
    X25519_KEY_SIZE,
)
from evnt.exceptions import SimpleSnowplowError

if TYPE_CHECKING:
    from evnt.config import EncryptionConfig

logger = structlog.get_logger(__name__)

try:
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
        X25519PublicKey,
    )
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the extra
    CRYPTOGRAPHY_AVAILABLE = False

    # Mirrors the name cryptography exports, so the error tuples below stay
    # importable without the extra; N818 does not apply to a stand-in for a
    # third-party class whose name we do not control.
    class UnsupportedAlgorithm(Exception):  # type: ignore[no-redef]  # noqa: N818
        """Placeholder for the real class when the `crypto` extra is absent."""


# Domain separation string. Changing it breaks every deployed client, so it is
# versioned alongside ENVELOPE_VERSION rather than derived from settings.
HKDF_INFO: Final[bytes] = b"evnt/e/v1"
AES_KEY_SIZE: Final[int] = 32
_PEM_PREFIX: Final[bytes] = b"-----BEGIN"
# Bound to a name because `ruff format --preview` strips the parentheses from
# an inline `except (A, B):` that has no `as` binding, producing a SyntaxError.
_KEY_LOAD_ERRORS: Final[tuple[type[Exception], ...]] = (
    ValueError,
    TypeError,
    UnsupportedAlgorithm,
)
# An over-large ceiling makes zlib raise OverflowError rather than zlib.error,
# which would turn an operator mistake into a 500 on every compressed request.
_DECOMPRESS_ERRORS: Final[tuple[type[Exception], ...]] = (zlib.error, OverflowError)
# +32 enables zlib/gzip header auto-detection; 15 is the maximum window size.
# Clients may send either container -- Android's Deflater emits zlib while
# GZIPOutputStream emits gzip, and neither team should have to care which.
_GZIP_WINDOW_BITS: Final[int] = 15 + 32
_GZIP_COMPRESS_WBITS: Final[int] = 16 + 15
_KID_ALPHABET: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-",
)
_MISSING_EXTRA_HINT: Final[str] = (
    "Encrypted ingest requires the `cryptography` package. "
    "Install it with the `crypto` extra: `uv sync --extra crypto` "
    "or `pip install evnt[crypto]`."
)


class EncryptionConfigError(SimpleSnowplowError):
    """Raised when the configured key material cannot be loaded."""


class DecryptionError(SimpleSnowplowError):
    """Raised when an envelope cannot be opened.

    Callers must map every instance to one uniform client-facing error: the
    reason travels in ``details`` for the server log only, never to the client,
    so a probing sender learns nothing from the response about which stage
    failed. Timing is deliberately not equalised -- an unknown key id returns
    before any key exchange -- because key ids ship inside the client bundle
    and are not secret.
    """


# Config failures arrive as either type: this module raises EncryptionConfigError,
# while EncryptionKeyConfig.resolve_material raises ValueError for an unreadable
# key file. An operator sees one kind of problem, so both are caught together.
_CONFIG_ERRORS: Final[tuple[type[Exception], ...]] = (EncryptionConfigError, ValueError)


def _require_cryptography() -> None:
    if not CRYPTOGRAPHY_AVAILABLE:
        raise EncryptionConfigError(_MISSING_EXTRA_HINT)


def _b64decode_any(value: str) -> bytes:
    """Decode standard or URL-safe base64, with or without padding."""
    text = "".join(value.split())
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("not valid base64") from exc


def parse_private_key(material: str | bytes) -> X25519PrivateKey:
    """
    Load an X25519 private key from any of the shapes operators hand us.

    Accepts PKCS#8 PEM, PKCS#8 DER, or the bare 32-byte scalar as raw bytes,
    base64 (standard or URL-safe), or hex. Operators paste key material from
    wildly different tooling, and guessing wrong is a startup-time failure with
    a confusing message, so all of these are supported deliberately.
    """
    _require_cryptography()

    if isinstance(material, str):
        text = material.strip()
        if text.startswith(_PEM_PREFIX.decode()):
            return _load_pem(text.encode())
        raw = _decode_scalar_text(text)
    else:
        raw = bytes(material)
        if raw.strip().startswith(_PEM_PREFIX):
            return _load_pem(raw)
        if len(raw) != X25519_KEY_SIZE:
            return _decode_binary_blob(raw)

    if len(raw) != X25519_KEY_SIZE:
        raise EncryptionConfigError(
            f"X25519 private key must be {X25519_KEY_SIZE} bytes, got {len(raw)}",
        )
    return X25519PrivateKey.from_private_bytes(raw)


def _load_pem(data: bytes) -> X25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except _KEY_LOAD_ERRORS as exc:
        raise EncryptionConfigError(f"invalid PEM private key: {exc}") from exc
    if not isinstance(key, X25519PrivateKey):
        raise EncryptionConfigError(
            f"expected an X25519 private key, got {type(key).__name__}",
        )
    return key


def _decode_scalar_text(text: str) -> bytes:
    """Decode a textual 32-byte scalar given as hex or base64."""
    compact = "".join(text.split())
    if len(compact) == X25519_KEY_SIZE * 2:
        try:
            return bytes.fromhex(compact)
        except ValueError:
            pass
    try:
        return _b64decode_any(compact)
    except ValueError as exc:
        raise EncryptionConfigError(
            "private key must be PEM, base64, or hex encoded",
        ) from exc


def _decode_binary_blob(raw: bytes) -> X25519PrivateKey:
    """Interpret a non-PEM, non-32-byte file as DER or as encoded text."""
    try:
        key = serialization.load_der_private_key(raw, password=None)
    except _KEY_LOAD_ERRORS:
        key = None
    if isinstance(key, X25519PrivateKey):
        return key
    if key is not None:
        raise EncryptionConfigError(
            f"expected an X25519 private key, got {type(key).__name__}",
        )
    try:
        return parse_private_key(raw.decode("ascii"))
    except UnicodeDecodeError as exc:
        raise EncryptionConfigError("unrecognised private key encoding") from exc


def public_key_bytes(private_key: X25519PrivateKey) -> bytes:
    """Return the raw 32-byte public key for a private key."""
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def generate_keypair() -> tuple[str, str]:
    """
    Generate a key pair for distribution.

    Returns:
        ``(private_key_b64, public_key_b64)`` -- both raw 32-byte keys in
        standard base64. The private half goes into the collector config; the
        public half is embedded in the mobile and web clients.
    """
    _require_cryptography()
    private_key = X25519PrivateKey.generate()
    private_raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return (
        base64.b64encode(private_raw).decode(),
        base64.b64encode(public_key_bytes(private_key)).decode(),
    )


def validate_kid(kid: str) -> str:
    """Validate a key id against the envelope's cleartext-ASCII constraint."""
    if not kid:
        raise ValueError("key id must not be empty")
    if len(kid) > ENVELOPE_MAX_KID_LEN:
        raise ValueError(f"key id must be at most {ENVELOPE_MAX_KID_LEN} characters")
    if not _KID_ALPHABET.issuperset(kid):
        raise ValueError("key id may only contain [A-Za-z0-9._-]")
    return kid


@dataclass(frozen=True, slots=True)
class KeyPair:
    """One decryption key, resolved from config at startup."""

    kid: str
    private_key: X25519PrivateKey
    public_key: bytes


@dataclass(frozen=True, slots=True)
class Envelope:
    """A parsed but still-sealed envelope."""

    version: int
    flags: int
    kid: str
    ephemeral_public_key: bytes
    nonce: bytes
    ciphertext: bytes
    aad: bytes

    @property
    def is_compressed(self) -> bool:
        return bool(self.flags & ENVELOPE_FLAG_GZIP)


class Keyring:
    """Immutable kid -> key pair lookup, built once during startup."""

    __slots__ = ("_keys",)

    def __init__(self, keys: dict[str, KeyPair]) -> None:
        # Copied so a caller mutating its own dict afterwards cannot swap a
        # decryption key under a running server.
        self._keys = dict(keys)

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(self._keys)

    @property
    def primary(self) -> KeyPair:
        """
        The key new clients should seal to.

        Config order is what makes rotation work without a flag day: put the
        new key first and clients adopt it as their cached copy of the browser
        sealer expires, while the old key stays live for everything already in
        flight or baked into a shipped app build.
        """
        try:
            return next(iter(self._keys.values()))
        except StopIteration:
            # `from_config` refuses to build an empty keyring, so this is only
            # reachable from a hand-assembled one in a test.
            raise EncryptionConfigError("keyring holds no keys") from None

    def get(self, kid: str) -> KeyPair | None:
        return self._keys.get(kid)

    @classmethod
    def from_config(cls, config: EncryptionConfig) -> Keyring:
        """
        Build a keyring from settings, failing fast on bad material.

        A misconfigured key must break startup rather than surface later as a
        stream of opaque 400s once traffic arrives.
        """
        _require_cryptography()

        keys: dict[str, KeyPair] = {}
        for entry in config.keys:
            if not entry.enabled:
                continue
            # Checked before parsing, so a duplicate entry reports the
            # duplicate rather than whatever is wrong with its key material.
            if entry.kid in keys:
                raise EncryptionConfigError(
                    f"duplicate encryption key id {entry.kid!r}",
                )
            try:
                private_key = parse_private_key(entry.resolve_material())
            except _CONFIG_ERRORS as exc:
                # resolve_material raises ValueError for an unreadable file;
                # both failures are the same thing to an operator.
                message = getattr(exc, "message", str(exc))
                raise EncryptionConfigError(
                    f"encryption key {entry.kid!r}: {message}",
                ) from exc
            keys[entry.kid] = KeyPair(
                kid=entry.kid,
                private_key=private_key,
                public_key=public_key_bytes(private_key),
            )

        if not keys:
            raise EncryptionConfigError(
                "encryption is enabled but no usable keys are configured; "
                "set EVNT_ENCRYPTION__KEYS or disable EVNT_ENCRYPTION__ENABLED",
            )
        return cls(keys)


def _header_size(kid_len: int) -> int:
    """Length of the authenticated header: magic .. epk inclusive."""
    return len(ENVELOPE_MAGIC) + 3 + kid_len + X25519_KEY_SIZE


def parse_envelope(data: bytes) -> Envelope:
    """
    Parse the binary envelope without touching any key material.

    Raises:
        DecryptionError: the bytes are not a well-formed envelope.
    """
    prefix_len = len(ENVELOPE_MAGIC) + 3
    if len(data) < prefix_len:
        raise DecryptionError("envelope truncated", {"stage": "header"})
    if not data.startswith(ENVELOPE_MAGIC):
        raise DecryptionError("bad envelope magic", {"stage": "magic"})

    version = data[4]
    if version != ENVELOPE_VERSION:
        raise DecryptionError(
            "unsupported envelope version",
            {"stage": "version", "version": version},
        )

    flags = data[5]
    if flags & ~ENVELOPE_FLAG_GZIP:
        raise DecryptionError(
            "unknown envelope flags",
            {"stage": "flags", "flags": flags},
        )

    kid_len = data[6]
    if not 1 <= kid_len <= ENVELOPE_MAX_KID_LEN:
        raise DecryptionError("bad key id length", {"stage": "kid_len"})

    header_end = _header_size(kid_len)
    min_len = header_end + AES_GCM_NONCE_SIZE + AES_GCM_TAG_SIZE
    if len(data) < min_len:
        raise DecryptionError("envelope truncated", {"stage": "body"})

    kid_end = prefix_len + kid_len
    try:
        kid = data[prefix_len:kid_end].decode("ascii")
    except UnicodeDecodeError as exc:
        raise DecryptionError("key id is not ASCII", {"stage": "kid"}) from exc
    if not _KID_ALPHABET.issuperset(kid):
        raise DecryptionError("key id has illegal characters", {"stage": "kid"})

    nonce_end = header_end + AES_GCM_NONCE_SIZE
    return Envelope(
        version=version,
        flags=flags,
        kid=kid,
        ephemeral_public_key=data[kid_end:header_end],
        nonce=data[header_end:nonce_end],
        ciphertext=data[nonce_end:],
        aad=data[:header_end],
    )


def _derive_key(
    private_key: X25519PrivateKey,
    recipient_public: bytes,
    ephemeral_public: bytes,
) -> bytes:
    try:
        peer = X25519PublicKey.from_public_bytes(ephemeral_public)
    except ValueError as exc:
        raise DecryptionError("bad ephemeral key", {"stage": "epk"}) from exc

    # A low-order peer point drives the shared secret to all zeroes, which the
    # sender can predict. OpenSSL already refuses it with a ValueError, but RFC
    # 7748 leaves the check to the caller, so the explicit test stays as a
    # backstop for backends that return the zero secret instead of raising.
    try:
        shared = private_key.exchange(peer)
    except ValueError as exc:
        raise DecryptionError("key exchange failed", {"stage": "kex"}) from exc
    if not any(shared):
        raise DecryptionError("degenerate shared secret", {"stage": "kex"})

    return HKDF(
        algorithm=hashes.SHA256(),
        length=AES_KEY_SIZE,
        salt=None,
        info=HKDF_INFO + ephemeral_public + recipient_public,
    ).derive(shared)


def _decompress(data: bytes, limit: int) -> bytes:
    """Gunzip with a hard output ceiling, so a bomb cannot exhaust memory."""
    # zlib reads max_length=0 as "unlimited", which would silently disable the
    # ceiling this function exists to enforce.
    if limit <= 0:
        # Server misconfiguration, not client input, so it must not travel the
        # DecryptionError path that the route turns into a 400.
        raise ValueError("decompression limit must be positive")
    decompressor = zlib.decompressobj(_GZIP_WINDOW_BITS)
    try:
        plaintext = decompressor.decompress(data, limit)
    except _DECOMPRESS_ERRORS as exc:
        raise DecryptionError(
            "malformed compressed payload",
            {"stage": "gzip"},
        ) from exc
    if decompressor.unconsumed_tail or not decompressor.eof:
        raise DecryptionError(
            "decompressed payload exceeds limit",
            {"stage": "gzip", "limit": limit},
        )
    return plaintext


def open_envelope(
    keyring: Keyring,
    data: bytes,
    max_plaintext_bytes: int,
) -> bytes:
    """
    Parse, decrypt, and optionally decompress a sealed payload.

    Args:
        keyring: Key pairs resolved at startup
        data: Raw envelope bytes
        max_plaintext_bytes: Ceiling on the decompressed plaintext

    Returns:
        The plaintext the client sealed -- for this endpoint, Snowplow JSON.

    Raises:
        DecryptionError: on any malformed, unauthentic, or oversized input.
    """
    _require_cryptography()

    envelope = parse_envelope(data)
    key_pair = keyring.get(envelope.kid)
    if key_pair is None:
        raise DecryptionError("unknown key id", {"stage": "kid", "kid": envelope.kid})

    key = _derive_key(
        key_pair.private_key,
        key_pair.public_key,
        envelope.ephemeral_public_key,
    )
    try:
        plaintext = AESGCM(key).decrypt(
            envelope.nonce,
            envelope.ciphertext,
            envelope.aad,
        )
    except Exception as exc:
        # InvalidTag and anything else the backend raises collapse into one
        # result on purpose: the sender must not learn why it failed.
        raise DecryptionError(
            "authentication failed",
            {"stage": "aead", "kid": envelope.kid},
        ) from exc

    if envelope.is_compressed:
        plaintext = _decompress(plaintext, max_plaintext_bytes)
    elif len(plaintext) > max_plaintext_bytes:
        raise DecryptionError(
            "plaintext exceeds limit",
            {"stage": "size", "limit": max_plaintext_bytes},
        )
    return plaintext


def seal_envelope(
    recipient_public: bytes,
    kid: str,
    plaintext: bytes,
    *,
    compress: bool = False,
) -> bytes:
    """
    Seal a payload the way a client must.

    The collector never needs to encrypt, so this exists as the executable
    specification of the wire format: the mobile and web implementations are
    correct exactly when they produce envelopes this function would produce,
    and the tests use it to avoid hand-rolling a second, drifting encryptor.

    Args:
        recipient_public: Raw 32-byte collector public key
        kid: Key id identifying that key
        plaintext: Payload to seal -- for this endpoint, Snowplow JSON
        compress: Gzip before sealing and set the corresponding flag

    Returns:
        The complete envelope, ready to be POSTed.
    """
    _require_cryptography()

    validate_kid(kid)
    if len(recipient_public) != X25519_KEY_SIZE:
        raise ValueError(f"recipient public key must be {X25519_KEY_SIZE} bytes")

    if compress:
        compressor = zlib.compressobj(wbits=_GZIP_COMPRESS_WBITS)
        plaintext = compressor.compress(plaintext) + compressor.flush()

    ephemeral = X25519PrivateKey.generate()
    epk = public_key_bytes(ephemeral)
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(recipient_public))
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=AES_KEY_SIZE,
        salt=None,
        info=HKDF_INFO + epk + recipient_public,
    ).derive(shared)

    kid_bytes = kid.encode("ascii")
    flags = ENVELOPE_FLAG_GZIP if compress else 0
    header = ENVELOPE_MAGIC + bytes((ENVELOPE_VERSION, flags, len(kid_bytes))) + kid_bytes + epk
    nonce = os.urandom(AES_GCM_NONCE_SIZE)
    return header + nonce + AESGCM(key).encrypt(nonce, plaintext, header)


def coerce_envelope_bytes(body: bytes) -> bytes:
    """
    Accept either the raw envelope or a base64 rendering of it.

    Mobile clients post ``application/octet-stream`` directly; browsers that
    route through ``sendBeacon`` or a text transport send base64. The magic
    prefix tells the two apart without trusting Content-Type.
    """
    if body.startswith(ENVELOPE_MAGIC):
        return body
    try:
        decoded = _b64decode_any(body.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise DecryptionError(
            "body is neither raw nor base64",
            {"stage": "transport"},
        ) from exc
    if not decoded.startswith(ENVELOPE_MAGIC):
        raise DecryptionError("bad envelope magic", {"stage": "magic"})
    return decoded
