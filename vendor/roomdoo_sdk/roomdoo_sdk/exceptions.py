class RoomdooSDKError(Exception):
    """Base exception for all SDK errors."""


class TransportError(RoomdooSDKError):
    """Communication error (HTTP, timeout, authentication)."""


class NotFoundError(RoomdooSDKError):
    """Record not found in Odoo."""


class ValidationError(RoomdooSDKError):
    """Invalid data provided."""


class ToolNotFoundError(RoomdooSDKError):
    """SDK tool method not found."""


class ToolExecutionError(RoomdooSDKError):
    """SDK tool execution failed."""
