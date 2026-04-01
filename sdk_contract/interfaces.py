"""
Expected public interface for the Roomdoo PMS SDK.

This module defines the contract that BookAI expects from the external
`roomdoo-sdk` Python package. It is NOT an implementation.

The real SDK will be developed in a separate repository with full
knowledge of the Odoo/Roomdoo PMS internals.
"""

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Data types returned by the SDK
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SDKProperty:
    """A hotel/property as known by Roomdoo."""

    id: int
    external_code: str
    name: str
    instance_url: str


@dataclass(frozen=True)
class SDKWhatsAppCredentials:
    """WhatsApp channel credentials for a given property."""

    phone_number_id: str   # Meta Phone Number ID
    access_token: str      # Meta permanent access token
    account_id: str        # Meta WhatsApp Business Account ID


@dataclass(frozen=True)
class SDKTemplate:
    """A WhatsApp template registered for a property."""

    code: str               # Internal code (e.g. "reserva_confirmation_v1")
    whatsapp_name: str      # Actual name on Meta platform
    language: str           # e.g. "es", "en"
    components: list[dict]  # Meta component structure


@dataclass(frozen=True)
class SDKFolio:
    """A reservation folio from the PMS."""

    id: int
    code: str              # e.g. "206/26/026072"
    checkin_date: date
    checkout_date: date
    property_id: int


# ---------------------------------------------------------------------------
# Client interface
# ---------------------------------------------------------------------------


@runtime_checkable
class RoomdooClientProtocol(Protocol):
    """
    Contract that BookAI expects from the Roomdoo SDK client.

    Implementations must be async-compatible and raise SDKError subclasses
    on expected failure conditions.
    """

    async def get_property(self, external_code: str) -> SDKProperty:
        """
        Resolve a property by its external code.
        Raises PropertyNotFound if the code is unknown.
        """
        ...

    async def get_whatsapp_credentials(self, property_id: int) -> SDKWhatsAppCredentials:
        """
        Return WhatsApp channel credentials for the given property.
        Raises CredentialsNotFound if the property has no WhatsApp channel configured.
        """
        ...

    async def get_template(self, code: str, language: str, property_id: int) -> SDKTemplate:
        """
        Return a WhatsApp template definition.
        Raises TemplateNotFound if not found for that property/language combination.
        """
        ...

    async def get_folio(self, folio_id: int) -> SDKFolio:
        """
        Return folio details by Roomdoo folio ID.
        Raises FolioNotFound if the folio does not exist.
        """
        ...


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SDKError(Exception):
    """Base exception for all Roomdoo SDK errors."""


class PropertyNotFound(SDKError):
    pass


class CredentialsNotFound(SDKError):
    pass


class TemplateNotFound(SDKError):
    pass


class FolioNotFound(SDKError):
    pass


class SDKAuthError(SDKError):
    """Raised when authentication against the Roomdoo instance fails."""
