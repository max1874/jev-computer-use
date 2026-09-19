"""A macOS computer-use agent with a dynamic, indexed action space."""

from .agent import Agent
from .desktop import Desktop, StaleWindow

__all__ = ["Agent", "Desktop", "StaleWindow"]
__version__ = "0.1.0"
