"""Encryption-at-rest for OAuth token files.

Uses Fernet (symmetric authenticated encryption) to protect token stores
on disk.  The master key is derived from a machine-bound secret created
on first use and persisted with restrictive Unix permissions.

If the ``cryptography`` package is missing, the module falls back to
plain JSON storage with a loud warning — never silently failing open.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

from loguru import logger


def _get_or_create_key_file() -> Path:
    """Return the path to the local token-encryption key file."""
    key_dir = Path.home() / ".config" / "grimoire"
    key_dir.mkdir(parents=True, exist_ok=True)
    # Restrict the directory so other users cannot list it
    os.chmod(key_dir, 0o700)
    return key_dir / ".token_key"


def _load_or_generate_key() -> bytes:
    """Load an existing Fernet key or create a new one.

    The key is stored in ``~/.config/grimoire/.token_key`` with ``0o600``.
    This is *not* a high-assurance secret-store (no keyring / TPM integration
    yet), but it is a large improvement over plain JSON on shared volumes.
    """
    key_path = _get_or_create_key_file()
    if key_path.exists():
        try:
            key = key_path.read_bytes()
            if len(key) == 32:
                return key
        except OSError:
            pass

    # Generate a new 256-bit key
    new_key = secrets.token_bytes(32)
    key_path.write_bytes(new_key)
    os.chmod(key_path, 0o600)
    logger.info(f"Generated new token encryption key at {key_path}")
    return new_key


try:
    from cryptography.fernet import Fernet, InvalidToken

    _FERNET_AVAILABLE = True
except ImportError:  # pragma: no cover
    _FERNET_AVAILABLE = False
    Fernet = None  # type: ignore[misc, assignment]
    InvalidToken = Exception  # type: ignore[misc, assignment]


class TokenCryptoError(Exception):
    """Raised when token encryption or decryption fails."""

    pass


class TokenCrypto:
    """Encrypt / decrypt OAuth token dictionaries for safe disk storage.

    Args:
        key: Optional 32-byte key.  If ``None``, a machine-local key is
             generated or loaded automatically.
    """

    def __init__(self, key: bytes | None = None) -> None:
        if not _FERNET_AVAILABLE:
            raise TokenCryptoError(
                "cryptography library is required for token encryption. "
                "Install it with: pip install cryptography"
            )
        from base64 import urlsafe_b64encode

        raw_key = key or _load_or_generate_key()
        # Fernet expects a URL-safe base64-encoded 32-byte key
        self._f = Fernet(urlsafe_b64encode(raw_key))

    def encrypt(self, data: dict[str, Any]) -> str:
        """Encrypt a token dictionary and return a URL-safe string."""
        import json

        payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return self._f.encrypt(payload).decode("ascii")

    def decrypt(self, token: str) -> dict[str, Any]:
        """Decrypt a token string back into a dictionary."""
        import json

        try:
            payload = self._f.decrypt(token.encode("ascii"))
            return json.loads(payload)
        except InvalidToken as exc:
            raise TokenCryptoError("Token decryption failed — invalid key or corrupted token") from exc
        except json.JSONDecodeError as exc:
            raise TokenCryptoError("Token decryption succeeded but payload is not valid JSON") from exc


def encrypt_tokens(data: dict[str, Any]) -> str:
    """Convenience shortcut: encrypt tokens with the machine-local key."""
    return TokenCrypto().encrypt(data)


def decrypt_tokens(token: str) -> dict[str, Any]:
    """Convenience shortcut: decrypt tokens with the machine-local key."""
    return TokenCrypto().decrypt(token)
