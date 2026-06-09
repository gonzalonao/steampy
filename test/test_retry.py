import pytest

from steampy.utils import retry_call


def test_returns_first_success() -> None:
    calls = []

    def action() -> str:
        calls.append(1)
        return "ok"

    assert retry_call(action, attempts=3) == "ok"
    assert len(calls) == 1


def test_retries_on_exception_then_succeeds() -> None:
    attempts = []

    def action() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise ValueError("boom")
        return "ok"

    assert retry_call(action, attempts=5, retry_exceptions=(ValueError,)) == "ok"
    assert len(attempts) == 3


def test_reraises_last_exception_when_exhausted() -> None:
    def action() -> str:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        retry_call(action, attempts=3, retry_exceptions=(ValueError,))


def test_retry_if_predicate() -> None:
    results = iter([1, 2, 3])

    def action() -> int:
        return next(results)

    # Retry while the result is below 3; the third call returns 3 and stops.
    assert retry_call(action, attempts=5, retry_if=lambda value: value < 3) == 3


def test_returns_last_result_even_if_still_matching() -> None:
    def action() -> int:
        return 0

    assert retry_call(action, attempts=2, retry_if=lambda value: value == 0) == 0


def test_invalid_attempts() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        retry_call(lambda: None, attempts=0)
