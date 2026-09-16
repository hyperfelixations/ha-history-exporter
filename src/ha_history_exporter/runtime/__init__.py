"""Run-time filesystem context: working directories, staging, locking."""

from .workspace import Workspace, cleanup_stale, open_workspace

__all__ = ["Workspace", "cleanup_stale", "open_workspace"]
