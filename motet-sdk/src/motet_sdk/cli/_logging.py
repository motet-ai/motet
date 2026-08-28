"""
Motet - CLI Logging Configuration

Shared logging configuration for all CLI modules.
Must be imported before any other CLI modules to ensure proper setup.
"""

import logging
import structlog
import click_log

# Create root logger and configure with click-log
logger = logging.getLogger()
click_log.basic_config(logger)

# Set default level to ERROR (suppress debug/info by default)
logger.setLevel(logging.ERROR)

# Reset structlog defaults to ensure clean state
try:
    structlog.reset_defaults()
except:
    pass

# Configure structlog to use Python logging backend
structlog.configure(
    wrapper_class=structlog.stdlib.BoundLogger,
    processors=[
        structlog.stdlib.filter_by_level,  # CRITICAL: Filter based on Python logging level
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer(),
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=False,  # Don't cache so our config takes effect
)

# Also set logging levels for known noisy loggers
logging.getLogger('motet.core.commands').setLevel(logging.ERROR)
logging.getLogger('motet.core.distributed').setLevel(logging.ERROR)


