"""
Platform adapters package for social media publishing.
"""

from .base import (
    BasePlatformAdapter,
    STATUS_WOULD_PUBLISH,
    STATUS_PUBLISHED,
    STATUS_FAILED,
    STATUS_SKIPPED
)
from .facebook_adapter import FacebookAdapter
from .instagram_adapter import InstagramAdapter
from .pinterest_adapter import PinterestAdapter
from .threads_adapter import ThreadsAdapter

__all__ = [
    "BasePlatformAdapter",
    "FacebookAdapter",
    "InstagramAdapter",
    "PinterestAdapter",
    "ThreadsAdapter",
    "STATUS_WOULD_PUBLISH",
    "STATUS_PUBLISHED",
    "STATUS_FAILED",
    "STATUS_SKIPPED",
]
