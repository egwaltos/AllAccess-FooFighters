"""
Structured logging setup with automated credential redaction.
Redacts Telegram Bot tokens, Discord webhook secrets, passwords, and authorization headers.
"""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sys
from typing import Optional, Union, List, Tuple


class CredentialRedactionFilter(logging.Filter):
    """
    Logging filter that intercepts and sanitizes sensitive credentials from log records
    (including messages, arguments, formatted exception tracebacks, and stack traces)
    before formatting and output.
    """

    PATTERNS: List[Tuple[re.Pattern, str]] = [
        # Telegram Bot Token in URLs: bot123456789:ABCdefGHIjklMNOpqrsTUVwxyz123456789
        (re.compile(r'bot\d+:[A-Za-z0-9_-]{20,}'), 'bot[REDACTED_TELEGRAM_TOKEN]'),
        # Standalone Telegram Bot Token: 123456789:ABCdefGHIjklMNOpqrsTUVwxyz123456789
        (re.compile(r'\b\d{6,14}:[A-Za-z0-9_-]{20,}\b'), '[REDACTED_TELEGRAM_TOKEN]'),
        # Discord Webhooks: /api/webhooks/1234567890/token-abc123xyz
        (re.compile(r'/api/webhooks/(\d+)/[A-Za-z0-9_-]+'), r'/api/webhooks/\1/[REDACTED_DISCORD_TOKEN]'),
        # URL Credentials (e.g. smtp://user:password@host or http://user:pass@host)
        (re.compile(r'(://[^:]+:)([^@]+)(@)'), r'\1[REDACTED]\3'),
        # Authorization Headers (Bearer, Basic)
        (re.compile(r'(?i)(Authorization:\s*(?:Bearer|Basic)\s+)[^\s,]+'), r'\1[REDACTED]'),
        # Key-Value assignments: password=..., secret: ..., api_key='...'
        (re.compile(r'(?i)(password|passwd|secret|api_key)\s*([:=])\s*([\'"][^\'"]+[\'"]|[^\s,;]+)'), r'\1\2[REDACTED]'),
    ]

    def __init__(self, secrets: Optional[List[str]] = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.secrets: List[str] = [str(s).strip() for s in (secrets or []) if s and str(s).strip()]

    def redact(self, text: str) -> str:
        """Applies explicit secret replacements followed by regex patterns."""
        if not text or not isinstance(text, str):
            return text
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        for pattern, replacement in self.PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        """
        Intercepts LogRecord and sanitizes pre-formatted messages, arguments,
        exception tracebacks (exc_info/exc_text), and stack_info.
        """
        msg = None
        if isinstance(getattr(record, "msg", None), str):
            if isinstance(getattr(record, "args", None), (tuple, dict)) and record.args:
                try:
                    formatted = record.getMessage()
                    if isinstance(formatted, str):
                        msg = formatted
                except Exception:
                    msg = record.msg
            else:
                msg = record.msg
        elif hasattr(record, "getMessage") and callable(record.getMessage):
            try:
                res = record.getMessage()
                if isinstance(res, str):
                    msg = res
            except Exception:
                pass

        if msg is not None:
            record.msg = self.redact(msg)
            if hasattr(record, "args") and isinstance(record.args, (tuple, dict)):
                record.args = ()

        # Sanitize exception traceback if present
        if isinstance(getattr(record, "exc_info", None), tuple):
            if not isinstance(getattr(record, "exc_text", None), str):
                formatter = logging.Formatter()
                record.exc_text = formatter.formatException(record.exc_info)
            if isinstance(record.exc_text, str):
                record.exc_text = self.redact(record.exc_text)
            record.exc_info = None
        elif isinstance(getattr(record, "exc_text", None), str):
            record.exc_text = self.redact(record.exc_text)

        # Sanitize stack info if present
        if getattr(record, "stack_info", None) and isinstance(record.stack_info, str):
            record.stack_info = self.redact(str(record.stack_info))

        return True


# Backward-compatible and E2E alias
RedactingFilter = CredentialRedactionFilter


def redact_credentials(text: str, secrets: Optional[List[str]] = None) -> str:
    """Convenience utility for sanitizing sensitive strings outside logging records."""
    redaction_filter = CredentialRedactionFilter(secrets=secrets)
    return redaction_filter.redact(text)


def setup_logging(
    level: str = "INFO",
    log_file: Optional[Union[str, Path]] = None,
    verbose: bool = False,
    quiet: bool = False,
    enable_redaction: bool = True,
    secrets: Optional[List[str]] = None,
) -> logging.Logger:
    """
    Configures structured logging for the allaccess_monitor application.
    Installs console handler and optional rotating file handler with credential redaction.
    """
    if verbose:
        resolved_level = logging.DEBUG
    elif quiet:
        resolved_level = logging.WARNING
    else:
        resolved_level = getattr(logging, level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(resolved_level)

    # Clean existing handlers to ensure idempotency across tests or multiple setups
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    redaction_filter = CredentialRedactionFilter(secrets=secrets) if enable_redaction else None

    # Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(resolved_level)
    if redaction_filter:
        console_handler.addFilter(redaction_filter)
    root_logger.addHandler(console_handler)

    # Rotating File Handler
    if log_file:
        file_path = Path(log_file)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            file_path,
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=3,
            encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(resolved_level)
        if redaction_filter:
            file_handler.addFilter(redaction_filter)
        root_logger.addHandler(file_handler)

    # Restrict noise from third-party libraries unless in verbose mode
    third_party_level = logging.DEBUG if verbose else logging.WARNING
    logging.getLogger("urllib3").setLevel(third_party_level)
    logging.getLogger("requests").setLevel(third_party_level)

    app_logger = logging.getLogger("allaccess_monitor")
    return app_logger
