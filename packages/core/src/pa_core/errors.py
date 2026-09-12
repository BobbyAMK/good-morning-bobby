"""Typed exception tree.

Why typed: the run harness decides whether to alert, retry, or fail silently based on
the exception class. String-matching error messages is how that logic rots.
"""

from __future__ import annotations


class PaError(Exception):
    """Base for everything this codebase raises deliberately."""


# --- configuration / setup problems: always a human's job to fix -------------
class ConfigError(PaError):
    pass


class MissingSecret(ConfigError):
    def __init__(self, name: str) -> None:
        super().__init__(f"secret {name!r} not in Keychain. Run: keyring set pet-agents {name}")
        self.name = name


class MigrationError(ConfigError):
    pass


# --- credentials: recoverable only by an interactive human -------------------
class GoogleAuthError(PaError):
    pass


class GoogleAuthMissing(GoogleAuthError):
    pass


class GoogleAuthExpired(GoogleAuthError):
    """Refresh token rejected: revoked, password changed, 7-day Testing expiry,
    6-month idle expiry, or pushed out by the 100-token-per-client-id cap."""


# --- LLM -------------------------------------------------------------------
class LLMError(PaError):
    pass


class LLMUnavailable(LLMError):
    """Backend unreachable (Ollama not running, network down). Retryable."""


class LLMBadOutput(LLMError):
    """Returned text did not validate against the requested schema. Retryable once."""


class NoEligibleBackend(LLMError):
    """No backend satisfies this call's privacy constraints -- see `sensitive`."""


class BudgetExceeded(LLMError):
    def __init__(self, spent: float, budget: float) -> None:
        super().__init__(f"month-to-date spend ${spent:.2f} >= budget ${budget:.2f}")
        self.spent, self.budget = spent, budget


# --- delivery --------------------------------------------------------------
class DeliveryError(PaError):
    pass


# --- harness ---------------------------------------------------------------
class AlreadyRunning(PaError):
    """Another process holds this agent's lock."""


class AlreadyRan(PaError):
    """This agent already has a run row for this logical_date."""


class RunTimeout(PaError):
    pass
