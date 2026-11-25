import os
import re
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

_VUL_PREFIX_RE = re.compile(r"^(?:вул\.\s*|вул\s+|вулиця\s+)", re.IGNORECASE)
_OTHER_STREET_PREFIX_RE = re.compile(
    r"^(?:(?:пр|просп|пров|бульв|пл|наб)\.|"
    r"(?:проспект|провулок|бульвар|площа|майдан|шосе|набережна)\b|б-р\b)\s*",
    re.IGNORECASE,
)


def _get_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    stripped = raw.strip()
    if not stripped:
        return default
    try:
        return int(stripped)
    except ValueError:
        return default


def _get_str_env(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip()


@dataclass
class Config:
    BOT_TOKEN: str = field(default_factory=lambda: _get_str_env("BOT_TOKEN", ""))
    CHECK_INTERVAL_SECONDS: int = field(
        default_factory=lambda: max(15, _get_int_env("CHECK_INTERVAL_SECONDS", 60))
    )

    # Target address configuration:
    # Бережани (Бережанська ОТГ), вул. Героїв Майдану, буд. 2-А
    OTG_ID: int = field(default_factory=lambda: _get_int_env("OTG_ID", 17))
    OTG_NAME: str = field(default_factory=lambda: _get_str_env("OTG_NAME", "Бережанська ОТГ"))

    CITY_ID: int = field(default_factory=lambda: _get_int_env("CITY_ID", 136))
    CITY_NAME: str = field(default_factory=lambda: _get_str_env("CITY_NAME", "Бережани"))

    STREET_ID: int = field(default_factory=lambda: _get_int_env("STREET_ID", 1663))
    STREET_NAME: str = field(default_factory=lambda: _get_str_env("STREET_NAME", "Героїв Майдану"))

    BUILDING_NAME: str = field(default_factory=lambda: _get_str_env("BUILDING_NAME", "2-А"))
    CHER_GPV: str = field(default_factory=lambda: _get_str_env("CHER_GPV", "2.1"))

    DB_PATH: str = field(default_factory=lambda: _get_str_env("DB_PATH", "bot_data.db") or "bot_data.db")
    DATABASE_URL: str = field(default_factory=lambda: _get_str_env("DATABASE_URL", ""))
    DB_SCHEMA: str = field(default_factory=lambda: _get_str_env("DB_SCHEMA", "bot_light") or "bot_light")
    TIMEZONE: str = field(default_factory=lambda: _get_str_env("TIMEZONE", "Europe/Kyiv") or "Europe/Kyiv")
    DEFAULT_CHAT_ID: int = field(default_factory=lambda: _get_int_env("DEFAULT_CHAT_ID", 0))
    PORT: int = field(default_factory=lambda: _get_int_env("PORT", 10000))
    RENDER_EXTERNAL_URL: str = field(default_factory=lambda: _get_str_env("RENDER_EXTERNAL_URL", "").rstrip("/"))
    RENDER_DEPLOY_HOOK_URL: str = field(default_factory=lambda: _get_str_env("RENDER_DEPLOY_HOOK_URL", ""))
    BOT_USERNAME: str = field(default_factory=lambda: _get_str_env("BOT_USERNAME", "").lstrip("@"))

    @property
    def clean_street(self) -> str:
        s = self.STREET_NAME.strip()
        return _VUL_PREFIX_RE.sub("", s, count=1).strip()

    @property
    def formatted_street(self) -> str:
        s = self.STREET_NAME.strip()
        if not s:
            return ""
        if _VUL_PREFIX_RE.match(s):
            return f"вул. {self.clean_street}"
        if _OTHER_STREET_PREFIX_RE.match(s):
            return s
        return f"вул. {s}"

    @property
    def short_address(self) -> str:
        return f"{self.formatted_street}, {self.BUILDING_NAME}"

    @property
    def full_address(self) -> str:
        return f"м. {self.CITY_NAME}, {self.formatted_street}, буд. {self.BUILDING_NAME}"


config = Config()
