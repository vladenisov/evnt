"""Unit tests for the sealed-envelope primitives behind the `/e` endpoint.

These pin the wire format the Android, iOS, and web clients have to match, so
a change that breaks deployed clients fails here first.
"""

import base64
import gzip
import zlib

import pytest
from evnt.config import EncryptionConfig, EncryptionKeyConfig
from evnt.constants import (
    ENVELOPE_MAGIC,
    MAX_ENCRYPTED_AMPLIFICATION,
    MAX_ENCRYPTED_ENVELOPE_LIMIT,
    MAX_KEY_FILE_BYTES,
)
from evnt.crypto import (
    DecryptionError,
    EncryptionConfigError,
    KeyPair,
    Keyring,
    _decompress,
    coerce_envelope_bytes,
    generate_keypair,
    open_envelope,
    parse_envelope,
    parse_private_key,
    public_key_bytes,
    seal_envelope,
    validate_kid,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

PLAINTEXT = b'{"schema":"iglu:test/jsonschema/1-0-0","data":[{"e":"pv"}]}'
LIMIT = 64 * 1024


@pytest.fixture
def keypair() -> tuple[str, bytes]:
    """Return a fresh (private_key_b64, public_key_bytes) pair."""
    private_b64, public_b64 = generate_keypair()
    return private_b64, base64.b64decode(public_b64)


@pytest.fixture
def keyring(keypair: tuple[str, bytes]) -> Keyring:
    private_b64, public_raw = keypair
    return Keyring({
        "k1": KeyPair("k1", parse_private_key(private_b64), public_raw),
    })


class TestRoundTrip:
    def test_seal_and_open(self, keypair, keyring):
        _, public_raw = keypair
        sealed = seal_envelope(public_raw, "k1", PLAINTEXT)

        assert open_envelope(keyring, sealed, LIMIT) == PLAINTEXT

    def test_compressed_round_trip(self, keypair, keyring):
        _, public_raw = keypair
        sealed = seal_envelope(public_raw, "k1", PLAINTEXT, compress=True)

        assert open_envelope(keyring, sealed, LIMIT) == PLAINTEXT

    @pytest.mark.parametrize(
        ("container", "compress"),
        [
            ("gzip", gzip.compress),
            ("zlib", zlib.compress),
        ],
    )
    def test_both_deflate_containers_are_accepted(self, container, compress):
        """Android's Deflater emits zlib while GZIPOutputStream emits gzip.

        Clients should not have to care which one their platform hands them.
        """
        assert _decompress(compress(PLAINTEXT), LIMIT) == PLAINTEXT

    def test_each_envelope_uses_a_fresh_ephemeral_key(self, keypair):
        _, public_raw = keypair
        first = parse_envelope(seal_envelope(public_raw, "k1", PLAINTEXT))
        second = parse_envelope(seal_envelope(public_raw, "k1", PLAINTEXT))

        assert first.ephemeral_public_key != second.ephemeral_public_key
        assert first.nonce != second.nonce

    def test_envelope_overhead_is_the_documented_69_bytes(self, keypair):
        """Mobile data budgets depend on this staying small and fixed."""
        _, public_raw = keypair
        sealed = seal_envelope(public_raw, "k1", PLAINTEXT)

        assert len(sealed) - len(PLAINTEXT) == 4 + 1 + 1 + 1 + 2 + 32 + 12 + 16


class TestTampering:
    @pytest.mark.parametrize(
        ("index", "field"),
        [(5, "flags"), (9, "epk"), (41, "nonce"), (-1, "ciphertext")],
    )
    def test_flipped_byte_fails_the_tag_check(self, keypair, keyring, index, field):
        """Every byte the AAD covers must fail authentication, not parse around it.

        A refactor that narrowed the AAD -- say, to stop covering the kid --
        would still parse these envelopes fine, so only the tag catches it.
        """
        _, public_raw = keypair
        sealed = bytearray(seal_envelope(public_raw, "k1", PLAINTEXT))
        sealed[index] ^= 0x01

        with pytest.raises(DecryptionError, match="authentication failed"):
            open_envelope(keyring, bytes(sealed), LIMIT)

    def test_swapping_the_kid_for_another_live_key_fails_the_tag_check(self):
        """The kid must be bound to the ciphertext, not just used for lookup.

        Two live keys whose ids differ by one bit: rewriting the envelope to
        name the other key parses and resolves fine, so only the AAD stops it.
        """
        first_private, first_public = generate_keypair()
        second_private, _ = generate_keypair()
        keyring = Keyring({
            "k0": KeyPair("k0", parse_private_key(second_private), b""),
            "k1": KeyPair(
                "k1",
                parse_private_key(first_private),
                base64.b64decode(first_public),
            ),
        })
        sealed = bytearray(
            seal_envelope(base64.b64decode(first_public), "k1", PLAINTEXT),
        )
        kid_last_byte = 4 + 3 + 1
        assert bytes(sealed[7:9]) == b"k1"
        sealed[kid_last_byte] ^= 0x01
        assert bytes(sealed[7:9]) == b"k0"

        with pytest.raises(DecryptionError, match="authentication failed"):
            open_envelope(keyring, bytes(sealed), LIMIT)

    def test_appended_byte_is_rejected(self, keypair, keyring):
        _, public_raw = keypair
        sealed = seal_envelope(public_raw, "k1", PLAINTEXT) + b"\x00"

        with pytest.raises(DecryptionError, match="authentication failed"):
            open_envelope(keyring, sealed, LIMIT)

    def test_wrong_key_is_rejected(self, keypair, keyring):
        """An envelope sealed to a different collector must not open."""
        _, other_public_b64 = generate_keypair()
        sealed = seal_envelope(base64.b64decode(other_public_b64), "k1", PLAINTEXT)

        with pytest.raises(DecryptionError, match="authentication failed"):
            open_envelope(keyring, sealed, LIMIT)

    def test_unknown_key_id_is_rejected(self, keypair, keyring):
        _, public_raw = keypair
        sealed = seal_envelope(public_raw, "nope", PLAINTEXT)

        with pytest.raises(DecryptionError, match="unknown key id"):
            open_envelope(keyring, sealed, LIMIT)

    def test_low_order_ephemeral_key_is_rejected(self, keypair, keyring):
        """A degenerate shared secret would be predictable by the sender."""
        _, public_raw = keypair
        sealed = bytearray(seal_envelope(public_raw, "k1", PLAINTEXT))
        epk_start = 4 + 3 + 2
        sealed[epk_start : epk_start + 32] = bytes(32)

        with pytest.raises(DecryptionError, match="key exchange failed"):
            open_envelope(keyring, bytes(sealed), LIMIT)


class TestMalformedEnvelopes:
    def test_empty_body(self):
        with pytest.raises(DecryptionError, match="truncated"):
            parse_envelope(b"")

    def test_bad_magic(self):
        with pytest.raises(DecryptionError, match="magic"):
            parse_envelope(b"XXXX" + bytes(80))

    def test_unsupported_version(self):
        with pytest.raises(DecryptionError, match="version"):
            parse_envelope(ENVELOPE_MAGIC + bytes([9, 0, 2]) + bytes(80))

    def test_unknown_flag_bits(self):
        with pytest.raises(DecryptionError, match="flags"):
            parse_envelope(ENVELOPE_MAGIC + bytes([1, 0x80, 2]) + bytes(80))

    def test_zero_length_kid(self):
        with pytest.raises(DecryptionError, match="key id length"):
            parse_envelope(ENVELOPE_MAGIC + bytes([1, 0, 0]) + bytes(80))

    def test_truncated_body(self, keypair):
        _, public_raw = keypair
        sealed = seal_envelope(public_raw, "k1", PLAINTEXT)

        with pytest.raises(DecryptionError, match="truncated"):
            parse_envelope(sealed[:40])


class TestSizeLimits:
    def test_oversized_plaintext_is_rejected(self, keypair, keyring):
        _, public_raw = keypair
        sealed = seal_envelope(public_raw, "k1", b"x" * 2048)

        with pytest.raises(DecryptionError, match="exceeds limit"):
            open_envelope(keyring, sealed, 1024)

    def test_compression_bomb_is_refused(self, keypair, keyring):
        """A tiny envelope must not be able to inflate past the ceiling."""
        _, public_raw = keypair
        sealed = seal_envelope(
            public_raw,
            "k1",
            b"\0" * (8 * 1024 * 1024),
            compress=True,
        )
        assert len(sealed) < 16 * 1024, "the bomb should arrive small"

        with pytest.raises(DecryptionError, match="exceeds limit"):
            open_envelope(keyring, sealed, 1024)


class TestTransportCoercion:
    def test_raw_bytes_pass_through(self, keypair):
        _, public_raw = keypair
        sealed = seal_envelope(public_raw, "k1", PLAINTEXT)

        assert coerce_envelope_bytes(sealed) == sealed

    @pytest.mark.parametrize("encoder", [base64.b64encode, base64.urlsafe_b64encode])
    def test_base64_is_decoded(self, keypair, encoder):
        _, public_raw = keypair
        sealed = seal_envelope(public_raw, "k1", PLAINTEXT)

        assert coerce_envelope_bytes(encoder(sealed)) == sealed

    def test_unpadded_base64_is_decoded(self, keypair):
        _, public_raw = keypair
        sealed = seal_envelope(public_raw, "k1", PLAINTEXT)
        encoded = base64.urlsafe_b64encode(sealed).rstrip(b"=")

        assert coerce_envelope_bytes(encoded) == sealed

    def test_garbage_is_rejected(self):
        with pytest.raises(DecryptionError):
            coerce_envelope_bytes(b"not an envelope at all!!!")


class TestKeyParsing:
    def test_base64_scalar(self, keypair):
        private_b64, public_raw = keypair

        assert public_key_bytes(parse_private_key(private_b64)) == public_raw

    def test_urlsafe_unpadded_scalar(self, keypair):
        private_b64, public_raw = keypair
        raw = base64.b64decode(private_b64)
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        assert public_key_bytes(parse_private_key(encoded)) == public_raw

    def test_hex_scalar(self, keypair):
        private_b64, public_raw = keypair
        hex_key = base64.b64decode(private_b64).hex()

        assert public_key_bytes(parse_private_key(hex_key)) == public_raw

    def test_pem_pkcs8(self, keypair):
        private_b64, public_raw = keypair
        key = X25519PrivateKey.from_private_bytes(base64.b64decode(private_b64))
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

        assert public_key_bytes(parse_private_key(pem)) == public_raw

    def test_raw_32_bytes(self, keypair):
        private_b64, public_raw = keypair

        assert (
            public_key_bytes(
                parse_private_key(base64.b64decode(private_b64)),
            )
            == public_raw
        )

    def test_wrong_length_is_rejected(self):
        with pytest.raises(EncryptionConfigError):
            parse_private_key(base64.b64encode(b"short").decode())

    def test_non_x25519_key_is_rejected(self):
        pem = (
            ed25519.Ed25519PrivateKey
            .generate()
            .private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            .decode()
        )

        with pytest.raises(EncryptionConfigError, match="expected an X25519"):
            parse_private_key(pem)


class TestKid:
    @pytest.mark.parametrize("kid", ["k1", "prod-2026.01", "A_b-C.9"])
    def test_accepted(self, kid):
        assert validate_kid(kid) == kid

    @pytest.mark.parametrize("kid", ["", "k 1", "k/1", "ключ", "x" * 33])
    def test_rejected(self, kid):
        with pytest.raises(ValueError):
            validate_kid(kid)


class TestKeyringFromConfig:
    def test_builds_from_inline_keys(self, keypair):
        private_b64, public_raw = keypair
        config = EncryptionConfig(
            enabled=True,
            keys=[EncryptionKeyConfig(kid="k1", private_key=private_b64)],
        )

        keyring = Keyring.from_config(config)

        assert keyring.key_ids == ("k1",)
        assert keyring.get("k1").public_key == public_raw

    def test_builds_from_key_file(self, keypair, tmp_path):
        private_b64, public_raw = keypair
        path = tmp_path / "k1.key"
        path.write_text(private_b64)
        config = EncryptionConfig(
            enabled=True,
            keys=[EncryptionKeyConfig(kid="k1", private_key_file=str(path))],
        )

        keyring = Keyring.from_config(config)

        assert keyring.get("k1").public_key == public_raw

    def test_multiple_keys_coexist(self, keypair):
        first_private, _ = keypair
        second_private, _ = generate_keypair()
        config = EncryptionConfig(
            enabled=True,
            keys=[
                EncryptionKeyConfig(kid="old", private_key=first_private),
                EncryptionKeyConfig(kid="new", private_key=second_private),
            ],
        )

        keyring = Keyring.from_config(config)

        assert set(keyring.key_ids) == {"old", "new"}

    def test_disabled_key_is_skipped(self, keypair):
        first_private, _ = keypair
        second_private, _ = generate_keypair()
        config = EncryptionConfig(
            enabled=True,
            keys=[
                EncryptionKeyConfig(kid="live", private_key=first_private),
                EncryptionKeyConfig(
                    kid="retired",
                    private_key=second_private,
                    enabled=False,
                ),
            ],
        )

        keyring = Keyring.from_config(config)

        assert keyring.key_ids == ("live",)

    def test_no_usable_keys_fails_fast(self):
        with pytest.raises(EncryptionConfigError, match="no usable keys"):
            Keyring.from_config(EncryptionConfig(enabled=True, keys=[]))

    def test_both_key_sources_is_rejected(self, keypair):
        private_b64, _ = keypair

        with pytest.raises(ValueError, match="exactly one"):
            EncryptionKeyConfig(
                kid="k1",
                private_key=private_b64,
                private_key_file="/tmp/nope",
            )

    def test_neither_key_source_is_rejected(self):
        with pytest.raises(ValueError, match="exactly one"):
            EncryptionKeyConfig(kid="k1")

    def test_bad_material_names_the_key(self, tmp_path):
        config = EncryptionConfig(
            enabled=True,
            keys=[EncryptionKeyConfig(kid="broken", private_key="!!!not-a-key!!!")],
        )

        with pytest.raises(EncryptionConfigError, match="broken"):
            Keyring.from_config(config)

    def test_private_key_is_not_in_the_repr(self, keypair):
        """SecretStr must keep key material out of logs and tracebacks."""
        private_b64, _ = keypair
        entry = EncryptionKeyConfig(kid="k1", private_key=private_b64)

        assert private_b64 not in repr(entry)


class TestDocumentedButPreviouslyUntested:
    """Behaviours the docstrings and config promise, pinned so they stay true."""

    def test_pkcs8_der_is_accepted(self, keypair):
        private_b64, public_raw = keypair
        der = X25519PrivateKey.from_private_bytes(
            base64.b64decode(private_b64),
        ).private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        assert public_key_bytes(parse_private_key(der)) == public_raw

    def test_pem_supplied_as_bytes_is_accepted(self, keypair):
        private_b64, public_raw = keypair
        pem = X25519PrivateKey.from_private_bytes(
            base64.b64decode(private_b64),
        ).private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        assert public_key_bytes(parse_private_key(pem)) == public_raw

    def test_duplicate_key_id_is_rejected(self, keypair):
        private_b64, _ = keypair
        other_private, _ = generate_keypair()
        config = EncryptionConfig(
            enabled=True,
            keys=[
                EncryptionKeyConfig(kid="k1", private_key=private_b64),
                EncryptionKeyConfig(kid="k1", private_key=other_private),
            ],
        )

        with pytest.raises(EncryptionConfigError, match="duplicate"):
            Keyring.from_config(config)

    def test_unreadable_key_file_is_an_encryption_config_error(self, tmp_path):
        """An unreadable file must not escape as a bare ValueError."""
        config = EncryptionConfig(
            enabled=True,
            keys=[
                EncryptionKeyConfig(
                    kid="k1",
                    private_key_file=str(tmp_path / "does-not-exist.key"),
                ),
            ],
        )

        with pytest.raises(EncryptionConfigError, match="k1"):
            Keyring.from_config(config)

    def test_oversized_key_file_is_refused(self, tmp_path):
        path = tmp_path / "huge.key"
        path.write_bytes(b"x" * (MAX_KEY_FILE_BYTES + 1))
        config = EncryptionConfig(
            enabled=True,
            keys=[EncryptionKeyConfig(kid="k1", private_key_file=str(path))],
        )

        with pytest.raises(EncryptionConfigError, match="k1"):
            Keyring.from_config(config)

    def test_valid_base64_with_wrong_magic_is_rejected(self):
        """Exercises the magic check after a successful base64 decode."""
        with pytest.raises(DecryptionError, match="magic"):
            coerce_envelope_bytes(base64.b64encode(b"XXXX" + bytes(80)))

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [("/e", "/e"), ("/e/", "/e"), ("  /custom  ", "/custom"), ("/", "/")],
    )
    def test_endpoint_is_normalised(self, configured, expected):
        assert EncryptionConfig(endpoint=configured).endpoint == expected

    def test_endpoint_must_be_rooted(self):
        with pytest.raises(ValueError, match="must start with"):
            EncryptionConfig(endpoint="e")

    def test_ceilings_are_capped(self):
        with pytest.raises(ValueError):
            EncryptionConfig(max_envelope_bytes=MAX_ENCRYPTED_ENVELOPE_LIMIT + 1)

    def test_amplification_ratio_is_bounded(self):
        """The gzip flag must not become a memory-amplification lever."""
        with pytest.raises(ValueError, match="max_plaintext_bytes"):
            EncryptionConfig(
                max_envelope_bytes=1024,
                max_plaintext_bytes=1024 * (MAX_ENCRYPTED_AMPLIFICATION + 1),
            )

    def test_oversized_decompression_limit_is_not_a_crash(self, keypair, keyring):
        """An absurd ceiling makes zlib raise OverflowError, not zlib.error."""
        _, public_raw = keypair
        sealed = seal_envelope(public_raw, "k1", PLAINTEXT, compress=True)

        with pytest.raises(DecryptionError, match="malformed compressed payload"):
            open_envelope(keyring, sealed, 10**30)
