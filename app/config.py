from dataclasses import dataclass, field
from os import environ


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./adrobot.db"
    campaign_prefix: str = "test_ryabov_"
    keitaro_base_url: str = ""
    keitaro_api_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=environ.get("DATABASE_URL", cls.database_url),
            campaign_prefix=environ.get("CAMPAIGN_PREFIX", cls.campaign_prefix),
            keitaro_base_url=environ.get("KEITARO_BASE_URL", "").strip(),
            keitaro_api_key=environ.get("KEITARO_API_KEY", "").strip(),
        )
