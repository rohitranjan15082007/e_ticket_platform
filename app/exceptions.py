"""Typed domain errors and their API representation."""

from dataclasses import dataclass


@dataclass(slots=True)
class AppError(Exception):
    code: str
    message: str
    status_code: int = 400


class AuthenticationError(AppError):
    def __init__(self, message: str = "Authentication failed") -> None:
        super().__init__("AUTHENTICATION_FAILED", message, 401)


class AuthorizationError(AppError):
    def __init__(self, message: str = "You do not have permission for this action") -> None:
        super().__init__("FORBIDDEN", message, 403)


class ConflictError(AppError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, 409)


class ValidationError(AppError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, 422)


class InsufficientFundsError(AppError):
    def __init__(self, message: str = "Wallet balance is insufficient") -> None:
        super().__init__("INSUFFICIENT_FUNDS", message, 409)


class InvariantViolationError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__("FINANCIAL_INVARIANT_VIOLATION", message, 409)
