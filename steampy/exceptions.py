"""Exceptions raised across the steampy library."""


class SevenDaysHoldException(Exception):
    """Raised when an account is under the 7-day trade hold (new device login)."""


class TooManyRequests(Exception):
    """Raised when Steam answers with HTTP 429 (rate limited)."""


class ApiException(Exception):
    """Raised when a Steam endpoint returns an unexpected or failed response."""


class LoginRequired(Exception):
    """Raised when an authenticated action is attempted before logging in."""


class InvalidCredentials(Exception):
    """Raised when login fails due to bad credentials or an invalid guard file."""


class CaptchaRequired(Exception):
    """Raised when Steam requires a captcha to complete the login."""


class ConfirmationExpected(Exception):
    """Raised when an expected mobile confirmation could not be found."""


class ProxyConnectionError(Exception):
    """Raised when a configured proxy cannot reach Steam."""
