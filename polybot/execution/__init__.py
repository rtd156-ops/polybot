"""Execution layer. One interface, two implementations (paper now, live gated)."""

from .base import ExecutionClient
from .paper import PaperExecution

__all__ = ["ExecutionClient", "PaperExecution"]
