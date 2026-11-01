"""
Configuration management, .env loading, validation, and secret masking.
Implements cascading configuration: CLI overrides > .env / environment variables > defaults.
"""
from dataclasses import dataclass, field
from pathlib import Path
import os
import re
from typing import Optional, List, Dict, Any, Union, Tuple

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

    # Target & Polling settings
    event_url = overrides.get("event_url") or os.getenv("ALLACCESS_EVENT_URL", "https://www.allaccess.com.ar/event/foo-fighters")
    target_section = overrides.get("target_section") or os.getenv("ALLACCESS_TARGET_SECTION", "Sivori media")

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
    )

    if enforce_validation:
        errors = config.validate()
        if errors:
            raise ConfigurationError(f"Configuration validation failed: {'; '.join(errors)}")

    return config
