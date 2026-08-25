"""Shared cancellation signal for long-running cleaning operations."""

from __future__ import annotations


class OperationCancelled(RuntimeError):
    """Raised when the user or service lifecycle asks work to stop."""
