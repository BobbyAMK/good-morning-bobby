"""Secrets come from the macOS Keychain, never from .env and never from a plist.

The honest caveat, from keyring's own docs: any Python script run from the SAME
executable can read these without the OS prompting. So Keychain protects against
exposure via disk, backups, and `git add -A` -- not against malicious local Python.
That is the right trade here, but it is a trade.

Secrets are deliberately NOT fields on Settings, so that:
  - no secret can be leaked by repr(settings) or a structlog event dict,
  - no secret is serialized into a run record,
  - importing Settings never touches the Keychain, so tests need no keychain at all.
"""

from __future__ import annotations

from typing import Final, Literal

import keyring

from .errors import MissingSecret

SERVICE: Final = "pet-agents"

SecretName = Literal[
    "github_token",
    "telegram_token",
    "coinmarketcap_key",
]


def get(name: SecretName) -> str:
    v = keyring.get_password(SERVICE, name)
    if not v:
        raise MissingSecret(name)
    return v


def get_optional(name: SecretName) -> str | None:
    return keyring.get_password(SERVICE, name)


def put(name: SecretName, value: str) -> None:
    keyring.set_password(SERVICE, name, value)


def delete(name: SecretName) -> None:
    try:
        keyring.delete_password(SERVICE, name)
    except keyring.errors.PasswordDeleteError:
        pass


def backend_name() -> str:
    return type(keyring.get_keyring()).__module__ + "." + type(keyring.get_keyring()).__qualname__
