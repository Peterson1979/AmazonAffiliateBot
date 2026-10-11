"""
Publishing package for AmazonAffiliateBot (V4).
Provides multi-platform adapters, target account configuration, and safe DRY_RUN simulation.
"""

from .config import PublishingConfig, DEFAULT_TARGET_ACCOUNTS
from .publisher import SocialPublisher, SUPPORTED_PLATFORMS
from .adapters import (
    BasePlatformAdapter,
    FacebookAdapter,
    InstagramAdapter,
    PinterestAdapter,
    ThreadsAdapter,
    STATUS_WOULD_PUBLISH,
    STATUS_PUBLISHED,
    STATUS_FAILED,
    STATUS_SKIPPED
)
from .queue import (
    PublishingQueue,
    QueueItem,
    QueueBuilder,
    QueuePublisher,
    get_budapest_timezone,
    parse_affiliate_urls,
    DEFAULT_QUEUE_FILE,
    DEFAULT_AFFILIATE_URLS_FILE,
    DEFAULT_LOGS_DIR,
    DEFAULT_LOG_FILE,
    DEFAULT_SCHEDULE_DAYS,
    DEFAULT_PRODUCTS_PER_DAY,
    DEFAULT_TIMEZONE,
    SLOT_1_TIME,
    SLOT_2_TIME,
    STATUS_QUEUED,
    STATUS_READY,
    STATUS_PUBLISHED,
    STATUS_PARTIALLY_PUBLISHED,
    STATUS_FAILED,
)
from .replenisher import (
    QueueReplenisher,
    DEFAULT_TARGET_FUTURE_CAPACITY,
    DEFAULT_MIN_FUTURE_CAPACITY,
    DEFAULT_MAINTENANCE_LOG_FILE,
)

__all__ = [
    "SocialPublisher",
    "PublishingConfig",
    "DEFAULT_TARGET_ACCOUNTS",
    "SUPPORTED_PLATFORMS",
    "BasePlatformAdapter",
    "FacebookAdapter",
    "InstagramAdapter",
    "PinterestAdapter",
    "ThreadsAdapter",
    "STATUS_WOULD_PUBLISH",
    "STATUS_PUBLISHED",
    "STATUS_FAILED",
    "STATUS_SKIPPED",
    "PublishingQueue",
    "QueueItem",
    "QueueBuilder",
    "QueuePublisher",
    "QueueReplenisher",
    "DEFAULT_TARGET_FUTURE_CAPACITY",
    "DEFAULT_MIN_FUTURE_CAPACITY",
    "DEFAULT_MAINTENANCE_LOG_FILE",
    "get_budapest_timezone",
    "parse_affiliate_urls",
    "DEFAULT_QUEUE_FILE",
    "DEFAULT_AFFILIATE_URLS_FILE",
    "DEFAULT_LOGS_DIR",
    "DEFAULT_LOG_FILE",
    "DEFAULT_SCHEDULE_DAYS",
    "DEFAULT_PRODUCTS_PER_DAY",
    "DEFAULT_TIMEZONE",
    "SLOT_1_TIME",
    "SLOT_2_TIME",
    "STATUS_QUEUED",
    "STATUS_READY",
    "STATUS_PARTIALLY_PUBLISHED",
]


