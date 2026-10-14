"""
Publishing configuration and target accounts for AmazonAffiliateBot (V4).
Defines target accounts, credentials loading from environment, and DRY_RUN mode.
Active supported platforms: Facebook, Pinterest, Threads. Legacy platform data is retained but not routed.
"""

import os
from typing import Dict, Any, Optional
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


DEFAULT_TARGET_ACCOUNTS = {
    "facebook": {
        "account": "LifeModeHQ"
    },
    "pinterest": {
        "account": "dailyideasforyou",
        "board": "Shopping ideas",
        "board_id": "1033646620665866548"
    },
    "threads": {
        "account": "lifemodehq"
    }
}

DEFAULT_PINTEREST_ACCOUNTS = {
    "dailyideasforyou": {
        "account": "dailyideasforyou",
        "board": "Shopping ideas",
        "board_id": "1033646620665866548",
    },
    "lifemodehq": {
        "account": "LifeModeHQ",
        "board": "Home & Everyday Living",
    },
}


@dataclass
class PublishingConfig:
    """Publishing configuration for social media platforms."""
    dry_run: bool = True
    accounts: Dict[str, Dict[str, Any]] = field(default_factory=lambda: dict(DEFAULT_TARGET_ACCOUNTS))
    credentials: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    pinterest_accounts: Dict[str, Dict[str, Any]] = field(
        default_factory=lambda: {key: dict(value) for key, value in DEFAULT_PINTEREST_ACCOUNTS.items()}
    )

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any], dry_run_override: Optional[bool] = None) -> "PublishingConfig":
        """Load publishing configuration from dictionary and environment variables."""
        pub_section = config_dict.get("publishing", {})

        # Explicit override takes highest precedence, followed by DRY_RUN env var, then config.json
        env_dry_run = os.environ.get("DRY_RUN", "").strip().lower()
        if dry_run_override is not None:
            dry_run = dry_run_override
        elif env_dry_run in ("true", "1", "yes"):
            dry_run = True
        elif env_dry_run in ("false", "0", "no"):
            dry_run = False
        else:
            dry_run = pub_section.get("dry_run", True)

        accounts = dict(DEFAULT_TARGET_ACCOUNTS)
        user_accounts = pub_section.get("accounts", {})
        for platform, acc_cfg in user_accounts.items():
            if platform in accounts and isinstance(acc_cfg, dict):
                accounts[platform].update(acc_cfg)

        pinterest_accounts = {
            key: dict(value) for key, value in DEFAULT_PINTEREST_ACCOUNTS.items()
        }
        configured_pinterest_accounts = pub_section.get("pinterest_accounts", {})
        if isinstance(configured_pinterest_accounts, dict):
            for account_key, account_cfg in configured_pinterest_accounts.items():
                if isinstance(account_cfg, dict):
                    pinterest_accounts.setdefault(account_key, {}).update(account_cfg)

        # Load credentials securely from environment variables only
        credentials = {
            "facebook": {
                "access_token": os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN") or os.environ.get("META_ACCESS_TOKEN", ""),
                "page_id": os.environ.get("FACEBOOK_PAGE_ID", "")
            },
            "pinterest": {
                "access_token": os.environ.get("PINTEREST_ACCESS_TOKEN", "")
            },
            "threads": {
                "access_token": os.environ.get("THREADS_ACCESS_TOKEN", ""),
                "user_id": os.environ.get("THREADS_USER_ID", "")
            }
        }

        credentials["pinterest_accounts"] = {
            "dailyideasforyou": {"access_token": credentials["pinterest"]["access_token"]},
            "lifemodehq": {
                "access_token": os.environ.get("PINTEREST_LIFEMODEHQ_ACCESS_TOKEN", "")
            },
        }

        return cls(
            dry_run=dry_run,
            accounts=accounts,
            credentials=credentials,
            pinterest_accounts=pinterest_accounts,
        )
