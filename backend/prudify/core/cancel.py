"""Shared cancellation signal for long-running cleaning operations."""

from __future__ import annotations


class OperationCancelled(RuntimeError):
    """Raised when the user or service lifecycle asks work to stop."""


class OperationPaused(RuntimeError):
    """Raised at a safe stopping point when work should wait and resume later.

    Distinct from cancellation: what has been done is kept, and the caller is
    expected to queue the work again rather than record it as stopped.
    """
