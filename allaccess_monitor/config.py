"""
Configuration management, .env loading, validation, and secret masking.
Implements cascading configuration: CLI overrides > .env / environment variables > defaults.
"""
from dataclasses import dataclass, field
from pathlib import Path
import json
import os
import re
from typing import Optional, List, Dict, Any, Union, Tuple

from allaccess_monitor.models import MonitoredTarget

try:
    from dotenv import load_dotenv
    _DOTENV_AVAILABLE = True
except ImportError:
    _DOTENV_AVAILABLE = False


class ConfigurationError(ValueError):
    """Raised when configuration validation fails."""
    pass


def parse_bool(val: Any, default: bool = False) -> bool:
    """Safely converts string, numeric or boolean representation to bool."""
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off", ""):
        return False
    return default


def parse_int(val: Any, default: int) -> int:
    """Safely converts value to int, raising ConfigurationError on invalid data."""
    if val is None or str(val).strip() == "":
        return default
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        raise ConfigurationError(f"Invalid integer value: {val!r}")


def parse_float(val: Any, default: float) -> float:
    """Safely converts value to float, raising ConfigurationError on invalid data."""
    if val is None or str(val).strip() == "":
        return default
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        raise ConfigurationError(f"Invalid float value: {val!r}")


def parse_comma_list(val: Optional[Union[str, List[str], Tuple[str, ...]]]) -> List[str]:
    """Parses a comma-separated string or sequence into a list of stripped strings."""
    if not val:
        return []
    if isinstance(val, (list, tuple)):
        return [str(item).strip() for item in val if str(item).strip()]
    return [item.strip() for item in str(val).split(",") if item.strip()]


def mask_secret(val: Optional[str]) -> str:
    """Masks a secret string, returning [REDACTED] or empty string if None/empty."""
    if not val:
        return ""
    return "[REDACTED]"


def derive_event_name_from_url(url: str) -> Optional[str]:
    """
    Derives a human-friendly event title from an AllAccess URL slug.
    Example: 'https://www.allaccess.com.ar/event/foo-fighters' -> 'Foo Fighters'
    """
    if not url:
        return None
    match = re.search(r'/event/([^/?#]+)', url)
    if match:
        slug = match.group(1).replace("-", " ").strip()
        return " ".join(word.capitalize() for word in slug.split())
    return None


def parse_targets_from_json(
    json_content: Union[str, bytes, List[Any], Dict[str, Any]],
    default_url: Optional[str] = None
) -> List[MonitoredTarget]:
    """
    Parses JSON data into a list of validated MonitoredTarget instances.
    Accepts raw JSON string/bytes or pre-parsed dict/list.
    """
    if isinstance(json_content, (str, bytes)):
        try:
            data = json.loads(json_content)
        except Exception as exc:
            raise ConfigurationError(f"Invalid JSON in events configuration: {exc}") from exc
    else:
        data = json_content

    if data is None:
        return []

    # Unwrap dictionary wrappers: {"events": [...]} or {"targets": [...]}
    if isinstance(data, dict):
        if "targets" in data:
            data = data["targets"]
        elif "events" in data:
            data = data["events"]
        else:
            # Map of {event_url: sections}
            data = [
                {"event_url": k, **(v if isinstance(v, dict) else {"sections": v})}
                for k, v in data.items()
            ]

    if not isinstance(data, list):
        raise ConfigurationError(f"Events JSON must be a list or dict with 'events'/'targets', got {type(data).__name__}")

    targets: List[MonitoredTarget] = []
    for item in data:
        if isinstance(item, MonitoredTarget):
            targets.append(item)
            continue
        if not isinstance(item, dict):
            raise ConfigurationError(f"Target entry must be a JSON object, got {type(item).__name__}")

        raw_url = item.get("event_url") or item.get("url") or default_url
        if not raw_url:
            raise ConfigurationError(f"Target entry missing required 'event_url': {item}")
        event_url = str(raw_url).strip()
        if not event_url:
            raise ConfigurationError(f"Target entry 'event_url' cannot be empty or whitespace: {item}")

        event_name = item.get("event_name") or item.get("name") or derive_event_name_from_url(event_url)

        max_price_raw = item.get("max_price")
        base_max_price = parse_float(max_price_raw, 0.0) if max_price_raw is not None else None
        if base_max_price is not None and base_max_price <= 0:
            raise ConfigurationError(f"Invalid max_price for '{event_url}': must be positive, got {base_max_price}")

        # Extract sections: supports 'target_section', 'target_sections', 'sections', 'section'
        raw_sections = (
            item.get("target_section")
            or item.get("target_sections")
            or item.get("sections")
            or item.get("section")
        )

        if not raw_sections or (isinstance(raw_sections, str) and not raw_sections.strip()):
            raise ConfigurationError(f"Target entry for '{event_url}' missing target sector definition.")

        section_entries: List[Tuple[str, Optional[float]]] = []
        if isinstance(raw_sections, (list, tuple)):
            for s in raw_sections:
                if isinstance(s, dict):
                    s_name = s.get("name") or s.get("section_name") or s.get("target_section")
                    s_price_raw = s.get("max_price")
                    s_price = parse_float(s_price_raw, 0.0) if s_price_raw is not None else base_max_price
                    if s_price is not None and s_price <= 0:
                        raise ConfigurationError(f"Invalid max_price for section '{s_name}': must be positive, got {s_price}")
                    clean_name = str(s_name).strip() if s_name is not None else ""
                    if clean_name:
                        section_entries.append((clean_name, s_price))
                    else:
                        raise ConfigurationError(f"Target section name in '{event_url}' cannot be empty or whitespace.")
                elif s is not None:
                    clean_s = str(s).strip()
                    if clean_s:
                        section_entries.append((clean_s, base_max_price))
                    else:
                        raise ConfigurationError(f"Target section in '{event_url}' cannot be empty or whitespace.")
        elif isinstance(raw_sections, str):
            for s_name in parse_comma_list(raw_sections):
                section_entries.append((s_name, base_max_price))

        if not section_entries:
            raise ConfigurationError(f"No valid target sections found for '{event_url}'.")

        for sec_name, price in section_entries:
            try:
                targets.append(MonitoredTarget(
                    event_url=event_url,
                    target_section=sec_name,
                    event_name=event_name,
                    max_price=price,
                ))
            except ValueError as ve:
                raise ConfigurationError(f"Invalid target entry: {ve}") from ve

    return targets


def parse_targets_from_file(file_path: Union[str, Path], default_url: Optional[str] = None) -> List[MonitoredTarget]:
    """Reads and parses an events JSON configuration file."""
    p = Path(file_path)
    if not p.is_file():
        raise ConfigurationError(f"Events file not found: {file_path}")
    try:
        content = p.read_text(encoding="utf-8")
    except Exception as exc:
        raise ConfigurationError(f"Could not read events file '{file_path}': {exc}") from exc
    return parse_targets_from_json(content, default_url=default_url)


@dataclass
class TelegramConfig:
    """Configuration settings for Telegram Bot notification channel."""
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    parse_mode: str = "HTML"
    timeout_seconds: float = 10.0

    def __repr__(self) -> str:
        if not self.bot_token:
            masked_token = ""
        elif len(self.bot_token) > 4:
            masked_token = f"{self.bot_token[:4]}***[REDACTED]"
        else:
            masked_token = "***[REDACTED]"
        return (f"TelegramConfig(enabled={self.enabled}, bot_token='{masked_token}', "
                f"chat_id='{self.chat_id}', parse_mode='{self.parse_mode}')")

    def is_valid(self) -> bool:
        """Returns True if the channel configuration has all required non-empty fields."""
        if not (self.bot_token.strip() and self.chat_id.strip()):
            return False
        return len(self.validate()) == 0

    def validate(self) -> List[str]:
        errors = []
        if self.enabled:
            if not self.bot_token.strip():
                errors.append("Telegram is enabled but 'bot_token' is empty.")
            if not self.chat_id.strip():
                errors.append("Telegram is enabled but 'chat_id' is empty.")
        return errors


@dataclass
class DiscordConfig:
    """Configuration settings for Discord Webhook notification channel."""
    enabled: bool = False
    webhook_url: str = ""
    username: str = "AllAccess Ticket Monitor"
    timeout_seconds: float = 10.0

    def __repr__(self) -> str:
        if not self.webhook_url:
            masked = ""
        else:
            masked = re.sub(r'(/api/webhooks/\d+/)[A-Za-z0-9_-]+', r'\1[REDACTED]', self.webhook_url)
            if masked == self.webhook_url:
                masked = f"{self.webhook_url[:35]}***[REDACTED]" if len(self.webhook_url) > 35 else "***[REDACTED]"
        return f"DiscordConfig(enabled={self.enabled}, webhook_url='{masked}', username='{self.username}')"

    def is_valid(self) -> bool:
        """Returns True if Discord webhook URL is non-empty and well-formed."""
        if not self.webhook_url.strip():
            return False
        return len(self.validate()) == 0

    def validate(self) -> List[str]:
        errors = []
        if self.enabled:
            if not self.webhook_url.strip():
                errors.append("Discord is enabled but 'webhook_url' is empty.")
            elif not self.webhook_url.startswith(("http://", "https://")):
                errors.append(f"Discord 'webhook_url' must start with http:// or https://: '{self.webhook_url}'")
        return errors


@dataclass
class EmailConfig:
    """Configuration settings for SMTP Email notification channel."""
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    use_tls: bool = True
    use_ssl: bool = False
    username: str = ""
    password: str = ""
    from_addr: str = ""
    to_addrs: List[str] = field(default_factory=list)
    timeout_seconds: float = 15.0

    def __post_init__(self):
        self.smtp_port = parse_int(self.smtp_port, 587)
        if isinstance(self.to_addrs, str):
            self.to_addrs = parse_comma_list(self.to_addrs)
        elif not isinstance(self.to_addrs, list):
            self.to_addrs = list(self.to_addrs) if self.to_addrs else []

    def __repr__(self) -> str:
        masked_pwd = "***" if self.password else ""
        return (f"EmailConfig(enabled={self.enabled}, host='{self.smtp_host}:{self.smtp_port}', "
                f"use_tls={self.use_tls}, use_ssl={self.use_ssl}, user='{self.username}', "
                f"password='{masked_pwd}', from='{self.from_addr}', to={self.to_addrs})")

    def is_valid(self) -> bool:
        """Returns True if Email settings have host, recipient, and valid port."""
        if not self.smtp_host.strip() or not self.to_addrs:
            return False
        return len(self.validate()) == 0

    def validate(self) -> List[str]:
        errors = []
        if self.enabled:
            if not self.smtp_host.strip():
                errors.append("Email is enabled but 'smtp_host' is empty.")
            if not (1 <= self.smtp_port <= 65535):
                errors.append(f"Email 'smtp_port' must be between 1 and 65535, got {self.smtp_port}.")
            if not self.to_addrs:
                errors.append("Email is enabled but 'to_addrs' is empty.")
            if not self.from_addr.strip() and not self.username.strip():
                errors.append("Email is enabled but neither 'from_addr' nor 'username' was provided.")
        return errors


@dataclass
class MonitorConfig:
    """Central application configuration coordinating checker, scheduler, and notifiers."""
    event_url: str = "https://www.allaccess.com.ar/event/foo-fighters"
    target_section: str = "Sivori media"
    check_interval_seconds: int = 60
    cooldown_seconds: int = 1800
    request_timeout_seconds: float = 10.0
    max_retries: int = 3
    notify_on_sold_out: bool = False
    state_file_path: Optional[str] = ".monitor_state.json"
    dry_run: bool = False
    once: bool = False
    mock_status: Optional[str] = None
    log_level: str = "INFO"
    log_file_path: Optional[str] = None
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    poll_interval: Optional[int] = None
    targets: List[MonitoredTarget] = field(default_factory=list)

    def __post_init__(self):
        if self.poll_interval is not None:
            if self.poll_interval <= 0:
                raise ValueError(f"poll_interval must be positive: {self.poll_interval}")
            self.check_interval_seconds = parse_int(self.poll_interval, 60)
        if self.event_url and self.event_url.startswith("ftp://") and not self.event_url.endswith(".url"):
            raise ValueError(f"Invalid URL scheme: {self.event_url}")
        self.check_interval_seconds = parse_int(self.check_interval_seconds, 60)
        self.poll_interval = self.check_interval_seconds
        self.cooldown_seconds = parse_int(self.cooldown_seconds, 1800)
        self.request_timeout_seconds = parse_float(self.request_timeout_seconds, 10.0)
        self.max_retries = parse_int(self.max_retries, 3)

        # Synchronize targets list with target_section & event_url
        if not self.targets:
            sec_list = parse_comma_list(self.target_section)
            if sec_list:
                self.targets = [
                    MonitoredTarget(
                        event_url=self.event_url,
                        target_section=sec,
                        event_name=derive_event_name_from_url(self.event_url) or "Foo Fighters",
                    )
                    for sec in sec_list
                ]
            elif self.target_section and self.target_section.strip():
                self.targets = [
                    MonitoredTarget(
                        event_url=self.event_url,
                        target_section=self.target_section.strip(),
                        event_name=derive_event_name_from_url(self.event_url) or "Foo Fighters",
                    )
                ]
        else:
            if self.targets:
                self.event_url = self.targets[0].event_url
                self.target_section = ", ".join(t.target_section for t in self.targets)

    @property
    def target_sections(self) -> List[str]:
        """Returns list of unique target section names across all monitored targets."""
        return list(dict.fromkeys(t.target_section for t in self.targets))

    @property
    def event_urls(self) -> List[str]:
        """Returns list of unique event URLs across all monitored targets."""
        return list(dict.fromkeys(t.event_url for t in self.targets))

    @property
    def is_multi_target(self) -> bool:
        """Returns True if monitoring more than one (event, section) pair."""
        return len(self.targets) > 1

    @property
    def has_active_notifiers(self) -> bool:
        """Returns True if any messaging notifier channel is enabled."""
        return self.telegram.enabled or self.discord.enabled or self.email.enabled

    def is_valid(self) -> bool:
        """Returns True if the monitor configuration is fully valid."""
        return len(self.validate()) == 0

    def validate(self) -> List[str]:
        """Validates all configuration settings and returns list of error messages."""
        errors = []
        if self.check_interval_seconds < 5:
            errors.append(f"'check_interval_seconds' ({self.check_interval_seconds}) must be at least 5 seconds.")
        if self.cooldown_seconds < 0:
            errors.append(f"'cooldown_seconds' ({self.cooldown_seconds}) cannot be negative.")
        if self.request_timeout_seconds <= 0:
            errors.append(f"'request_timeout_seconds' ({self.request_timeout_seconds}) must be positive.")
        if not self.event_url.startswith(("http://", "https://")):
            errors.append(f"'event_url' must start with http:// or https://: '{self.event_url}'")
        if not self.target_section.strip():
            errors.append("'target_section' cannot be empty.")

        # Target validation
        if not self.targets:
            errors.append("At least one monitored target must be configured.")
        else:
            for i, target in enumerate(self.targets):
                if not target.event_url or not target.event_url.startswith(("http://", "https://")):
                    errors.append(f"Target [{i}] 'event_url' must start with http:// or https://: '{target.event_url}'")
                if not target.target_section or not target.target_section.strip():
                    errors.append(f"Target [{i}] 'target_section' cannot be empty.")
                if target.max_price is not None and target.max_price <= 0:
                    errors.append(f"Target [{i}] 'max_price' must be positive, got {target.max_price}.")

        errors.extend(self.telegram.validate())
        errors.extend(self.discord.validate())
        errors.extend(self.email.validate())
        return errors

    def build_notifier(self) -> "CompositeNotifier":
        """
        Builds and returns a CompositeNotifier instance containing all enabled notification channels.
        If dry_run is True or no external channels are enabled, includes ConsoleNotifier.
        """
        from allaccess_monitor.notifier.telegram import TelegramNotifier
        from allaccess_monitor.notifier.discord import DiscordNotifier
        from allaccess_monitor.notifier.email import EmailNotifier
        from allaccess_monitor.notifier.console import ConsoleNotifier
        from allaccess_monitor.notifier.composite import CompositeNotifier

        if self.dry_run:
            return CompositeNotifier([ConsoleNotifier()])

        notifiers = []
        if self.telegram.enabled:
            notifiers.append(TelegramNotifier(
                bot_token=self.telegram.bot_token,
                chat_id=self.telegram.chat_id,
                parse_mode=self.telegram.parse_mode,
                timeout=self.telegram.timeout_seconds,
            ))

        if self.discord.enabled:
            notifiers.append(DiscordNotifier(
                webhook_url=self.discord.webhook_url,
                username=self.discord.username,
                timeout=self.discord.timeout_seconds,
            ))

        if self.email.enabled:
            notifiers.append(EmailNotifier(
                smtp_host=self.email.smtp_host,
                smtp_port=self.email.smtp_port,
                username=self.email.username,
                password=self.email.password,
                from_addr=self.email.from_addr,
                to_addrs=self.email.to_addrs,
                use_tls=self.email.use_tls,
                use_ssl=self.email.use_ssl,
                timeout=self.email.timeout_seconds,
            ))

        if not notifiers:
            notifiers.append(ConsoleNotifier())

        return CompositeNotifier(notifiers)

    @classmethod
    def from_env(cls, env_file: Optional[Union[str, Path]] = None, **kwargs) -> "MonitorConfig":
        """Loads configuration from environment variables or specified .env file."""
        return load_config(env_file=env_file, overrides=kwargs if kwargs else None)

    @classmethod
    def from_args(cls, args: Any, **kwargs) -> "MonitorConfig":
        """Builds MonitorConfig from parsed CLI arguments (Namespace or dict)."""
        overrides = dict(kwargs)
        if isinstance(args, dict):
            getter = args.get
        else:
            getter = lambda k, d=None: getattr(args, k, d)

        def _first_not_none(*keys: str) -> Any:
            for k in keys:
                val = getter(k)
                if val is not None:
                    return val
            return None

        env_file = _first_not_none("config", "env_file")

        mapping = {
            "check_interval_seconds": _first_not_none("interval", "check_interval_seconds", "poll_interval"),
            "cooldown_seconds": _first_not_none("cooldown", "cooldown_seconds"),
            "target_section": _first_not_none("section", "target_section"),
            "event_url": _first_not_none("url", "event_url"),
            "request_timeout_seconds": _first_not_none("timeout", "request_timeout_seconds"),
            "max_retries": _first_not_none("retries", "max_retries"),
            "dry_run": getter("dry_run"),
            "once": getter("once"),
            "mock_status": getter("mock_status"),
            "log_level": getter("log_level"),
            "log_file_path": _first_not_none("log_file", "log_file_path"),
            "events_file": _first_not_none("events_file", "events"),
            "events_json": getter("events_json"),
            "targets": getter("targets"),
        }

        for k, v in mapping.items():
            if v is not None and k not in overrides:
                overrides[k] = v

        return load_config(env_file=env_file, overrides=overrides)


def load_config(
    env_file: Optional[Union[str, Path]] = None,
    overrides: Optional[Dict[str, Any]] = None,
    enforce_validation: bool = True
) -> MonitorConfig:
    """
    Loads configuration from environment variables (optionally loading .env file first),
    applies runtime overrides with strict type coercion, and validates the configuration.
    """
    if env_file:
        for k in (
            "POLL_INTERVAL", "POLL_INTERVAL_SECONDS", "ALLACCESS_TARGET_SECTION",
            "ALLACCESS_EVENT_URL", "ALERT_COOLDOWN", "ALERT_COOLDOWN_SECONDS",
            "REQUEST_TIMEOUT", "REQUEST_TIMEOUT_SECONDS", "MAX_RETRIES",
            "NOTIFY_ON_SOLD_OUT", "STATE_FILE_PATH", "DRY_RUN", "ONCE",
            "LOG_LEVEL", "LOG_FILE_PATH", "TELEGRAM_ENABLED", "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID", "TELEGRAM_PARSE_MODE", "TELEGRAM_TIMEOUT_SECONDS",
            "DISCORD_ENABLED", "DISCORD_WEBHOOK_URL", "DISCORD_USERNAME",
            "DISCORD_TIMEOUT_SECONDS", "EMAIL_ENABLED", "SMTP_HOST", "SMTP_PORT",
            "SMTP_USE_TLS", "SMTP_USE_SSL", "SMTP_USERNAME", "SMTP_PASSWORD",
            "EMAIL_FROM", "EMAIL_TO", "EMAIL_TIMEOUT_SECONDS",
            "ALLACCESS_EVENTS_JSON", "ALLACCESS_EVENTS_FILE", "EVENTS_FILE",
        ):
            os.environ.pop(k, None)

        if _DOTENV_AVAILABLE:
            load_dotenv(dotenv_path=env_file, override=True)
        else:
            p = Path(env_file)
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ[k.strip()] = v.strip().strip("'\"")
    else:
        if _DOTENV_AVAILABLE:
            load_dotenv(override=False)

    overrides = overrides or {}

    event_url = (
        overrides.get("event_url")
        or overrides.get("url")
        or os.getenv("ALLACCESS_EVENT_URL", "https://www.allaccess.com.ar/event/foo-fighters")
    )
    target_section = (
        overrides.get("target_section")
        or overrides.get("section")
        or os.getenv("ALLACCESS_TARGET_SECTION", "Sivori media")
    )

    poll_override = overrides.get("poll_interval") if overrides.get("poll_interval") is not None else overrides.get("check_interval_seconds")
    if poll_override is not None:
        check_interval = parse_int(poll_override, 60)
    else:
        check_interval = parse_int(os.getenv("POLL_INTERVAL") or os.getenv("POLL_INTERVAL_SECONDS"), 60)

    cooldown_override = overrides.get("cooldown_seconds") if overrides.get("cooldown_seconds") is not None else overrides.get("cooldown")
    if cooldown_override is not None:
        cooldown = parse_int(cooldown_override, 1800)
    else:
        cooldown = parse_int(os.getenv("ALERT_COOLDOWN") or os.getenv("ALERT_COOLDOWN_SECONDS"), 1800)

    req_timeout_override = overrides.get("request_timeout_seconds") if overrides.get("request_timeout_seconds") is not None else overrides.get("timeout")
    if req_timeout_override is not None:
        request_timeout = parse_float(req_timeout_override, 10.0)
    else:
        request_timeout = parse_float(os.getenv("REQUEST_TIMEOUT") or os.getenv("REQUEST_TIMEOUT_SECONDS"), 10.0)

    max_retries_override = overrides.get("max_retries") if overrides.get("max_retries") is not None else overrides.get("retries")
    if max_retries_override is not None:
        max_retries = parse_int(max_retries_override, 3)
    else:
        max_retries = parse_int(os.getenv("MAX_RETRIES"), 3)

    notify_on_sold_out_override = overrides.get("notify_on_sold_out")
    if notify_on_sold_out_override is not None:
        notify_on_sold_out = parse_bool(notify_on_sold_out_override)
    else:
        notify_on_sold_out = parse_bool(os.getenv("NOTIFY_ON_SOLD_OUT"), False)

    state_file_path = overrides.get("state_file_path", os.getenv("STATE_FILE_PATH", ".monitor_state.json"))
    dry_run = parse_bool(overrides.get("dry_run", os.getenv("DRY_RUN", False)))
    once = parse_bool(overrides.get("once", os.getenv("ONCE", False)))
    mock_status = overrides.get("mock_status", os.getenv("MOCK_STATUS"))
    log_level = str(overrides.get("log_level", os.getenv("LOG_LEVEL", "INFO"))).upper()
    log_file_path = overrides.get("log_file_path", os.getenv("LOG_FILE_PATH"))

    # Telegram
    tg_token = str(overrides.get("telegram_bot_token") or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    tg_chat = str(overrides.get("telegram_chat_id") or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    tg_env_enabled = os.getenv("TELEGRAM_ENABLED")
    if "telegram_enabled" in overrides:
        tg_enabled = parse_bool(overrides["telegram_enabled"])
    elif tg_env_enabled is not None:
        tg_enabled = parse_bool(tg_env_enabled)
    else:
        tg_enabled = bool(tg_token and tg_chat)

    telegram_cfg = TelegramConfig(
        enabled=tg_enabled,
        bot_token=tg_token,
        chat_id=tg_chat,
        parse_mode=overrides.get("telegram_parse_mode", os.getenv("TELEGRAM_PARSE_MODE", "HTML")),
        timeout_seconds=parse_float(overrides.get("telegram_timeout_seconds", os.getenv("TELEGRAM_TIMEOUT_SECONDS")), 10.0)
    )

    # Discord
    dc_url = str(overrides.get("discord_webhook_url") or os.getenv("DISCORD_WEBHOOK_URL", "")).strip()
    dc_env_enabled = os.getenv("DISCORD_ENABLED")
    if "discord_enabled" in overrides:
        dc_enabled = parse_bool(overrides["discord_enabled"])
    elif dc_env_enabled is not None:
        dc_enabled = parse_bool(dc_env_enabled)
    else:
        dc_enabled = bool(dc_url)

    discord_cfg = DiscordConfig(
        enabled=dc_enabled,
        webhook_url=dc_url,
        username=overrides.get("discord_username", os.getenv("DISCORD_USERNAME", "AllAccess Ticket Monitor")),
        timeout_seconds=parse_float(overrides.get("discord_timeout_seconds", os.getenv("DISCORD_TIMEOUT_SECONDS")), 10.0)
    )

    # Email
    smtp_host = str(overrides.get("smtp_host") or os.getenv("SMTP_HOST", "")).strip()
    smtp_port_override = overrides.get("smtp_port")
    if smtp_port_override is not None:
        smtp_port = parse_int(smtp_port_override, 587)
    else:
        smtp_port = parse_int(os.getenv("SMTP_PORT"), 587)

    use_tls_override = overrides.get("smtp_use_tls") if overrides.get("smtp_use_tls") is not None else os.getenv("SMTP_USE_TLS")
    use_tls = parse_bool(use_tls_override, True)

    use_ssl_override = overrides.get("smtp_use_ssl") if overrides.get("smtp_use_ssl") is not None else os.getenv("SMTP_USE_SSL")
    use_ssl = parse_bool(use_ssl_override, False)

    username = str(overrides.get("smtp_username") or os.getenv("SMTP_USERNAME", "")).strip()
    password = str(overrides.get("smtp_password") or os.getenv("SMTP_PASSWORD", "")).strip()
    from_addr = str(overrides.get("email_from") or os.getenv("EMAIL_FROM", "")).strip() or username

    to_addrs_override = overrides.get("email_to") if overrides.get("email_to") is not None else overrides.get("to_addrs")
    if to_addrs_override is not None:
        if isinstance(to_addrs_override, str):
            to_addrs = parse_comma_list(to_addrs_override)
        elif isinstance(to_addrs_override, (list, tuple)):
            to_addrs = list(to_addrs_override)
        else:
            to_addrs = []
    else:
        to_addrs = parse_comma_list(os.getenv("EMAIL_TO"))

    em_env_enabled = os.getenv("EMAIL_ENABLED")
    if "email_enabled" in overrides:
        em_enabled = parse_bool(overrides["email_enabled"])
    elif em_env_enabled is not None:
        em_enabled = parse_bool(em_env_enabled)
    else:
        em_enabled = bool(smtp_host and to_addrs)

    email_cfg = EmailConfig(
        enabled=em_enabled,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        use_tls=use_tls,
        use_ssl=use_ssl,
        username=username,
        password=password,
        from_addr=from_addr,
        to_addrs=to_addrs,
        timeout_seconds=parse_float(overrides.get("email_timeout_seconds", os.getenv("EMAIL_TIMEOUT_SECONDS")), 15.0)
    )

    # Resolve monitored targets following strict cascading precedence:
    # Tier 1 (Highest): Explicit CLI / Runtime Overrides
    #   1a. overrides["targets"]
    #   1b. overrides["events_json"]
    #   1c. overrides["events_file"]
    #   1d. overrides["target_section"] / overrides["event_url"] (-s, -u)
    # Tier 2 (Middle): Environment Variables (.env / os.environ)
    #   2a. ALLACCESS_EVENTS_JSON
    #   2b. ALLACCESS_EVENTS_FILE / EVENTS_FILE
    #   2c. ALLACCESS_TARGET_SECTION (via target_section / __post_init__)
    # Tier 3 (Fallback): Local Configuration File
    #   3a. events.json (only if no section/target was specified in env or overrides)
    # Tier 4 (Lowest): Built-in Defaults (Foo Fighters / Sivori media)

    targets: Optional[List[MonitoredTarget]] = None

    # Tier 1a: Explicit targets override
    if "targets" in overrides and overrides["targets"] is not None:
        raw_t = overrides["targets"]
        if isinstance(raw_t, (str, bytes)):
            targets = parse_targets_from_json(raw_t, default_url=event_url)
        elif isinstance(raw_t, list):
            targets = [
                t if isinstance(t, MonitoredTarget) else MonitoredTarget.from_dict(t) if isinstance(t, dict) else t
                for t in raw_t
            ]

    # Tier 1b: Explicit CLI events_json override
    if targets is None and "events_json" in overrides and overrides["events_json"]:
        targets = parse_targets_from_json(overrides["events_json"], default_url=event_url)

    # Tier 1c: Explicit CLI events_file override
    if targets is None and "events_file" in overrides and overrides["events_file"]:
        targets = parse_targets_from_file(overrides["events_file"], default_url=event_url)

    # Tier 1d: Explicit CLI single-target override (-s / --section, -u / --url)
    # Explicit CLI flags must strictly override ambient environment variables
    # (ALLACCESS_EVENTS_JSON, ALLACCESS_EVENTS_FILE).
    has_sec_cli = bool(
        (overrides.get("target_section") and str(overrides.get("target_section")).strip())
        or (overrides.get("section") and str(overrides.get("section")).strip())
    )
    has_url_cli = bool(
        (overrides.get("event_url") and str(overrides.get("event_url")).strip())
        or (overrides.get("url") and str(overrides.get("url")).strip())
    )
    if targets is None and (has_sec_cli or has_url_cli):
        sec_list = parse_comma_list(target_section)
        if sec_list:
            targets = [
                MonitoredTarget(
                    event_url=event_url,
                    target_section=sec,
                    event_name=derive_event_name_from_url(event_url) or "Foo Fighters",
                )
                for sec in sec_list
            ]
        elif target_section and target_section.strip():
            targets = [
                MonitoredTarget(
                    event_url=event_url,
                    target_section=target_section.strip(),
                    event_name=derive_event_name_from_url(event_url) or "Foo Fighters",
                )
            ]

    # Tier 2: Environment Variables (only consulted if NO CLI target overrides were supplied)
    if targets is None:
        env_events_json = os.getenv("ALLACCESS_EVENTS_JSON")
        if env_events_json and env_events_json.strip():
            targets = parse_targets_from_json(env_events_json, default_url=event_url)

    if targets is None:
        env_events_file = (
            os.getenv("ALLACCESS_EVENTS_FILE")
            or os.getenv("EVENTS_FILE")
        )
        if env_events_file and env_events_file.strip():
            targets = parse_targets_from_file(env_events_file, default_url=event_url)

    # Tier 3: Local configuration file (events.json)
    if targets is None:
        has_sec_env = "ALLACCESS_TARGET_SECTION" in os.environ
        if not has_sec_cli and not has_sec_env:
            local_events = Path("events.json")
            if local_events.is_file():
                try:
                    targets = parse_targets_from_file(local_events, default_url=event_url)
                except Exception:
                    targets = None

    config = MonitorConfig(
        event_url=event_url,
        target_section=target_section,
        check_interval_seconds=check_interval,
        cooldown_seconds=cooldown,
        request_timeout_seconds=request_timeout,
        max_retries=max_retries,
        notify_on_sold_out=notify_on_sold_out,
        state_file_path=state_file_path,
        dry_run=dry_run,
        once=once,
        mock_status=mock_status,
        log_level=log_level,
        log_file_path=log_file_path,
        telegram=telegram_cfg,
        discord=discord_cfg,
        email=email_cfg,
        poll_interval=check_interval,
        targets=targets if targets is not None else [],
    )

    if enforce_validation:
        errors = config.validate()
        if errors:
            raise ConfigurationError(f"Configuration validation failed: {'; '.join(errors)}")

    return config
