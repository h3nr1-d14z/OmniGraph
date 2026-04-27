"""Structlog configuration for OmniGraph Hub.

Call ``configure_logging()`` once at process startup (before any loggers are
created) to install the shared processor chain.  TTY-aware: ConsoleRenderer
in dev (stderr is a TTY), JSONRenderer in non-TTY / Docker.

Uvicorn's own loggers are wrapped so their output flows through the same
pipeline.
"""

import logging
import logging.config
import sys

import structlog


def configure_logging(json_logs: bool = True) -> None:
    """Configure structlog + stdlib logging with a shared processor chain.

    Parameters
    ----------
    json_logs:
        ``True`` → JSONRenderer (production / Docker).
        ``False`` → ConsoleRenderer (local dev, TTY).
        Callers typically pass ``not sys.stderr.isatty()``.
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if json_logs:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stderr",
                    "formatter": "structlog",
                },
            },
            "formatters": {
                "structlog": {
                    "()": structlog.stdlib.ProcessorFormatter,
                    "processor": renderer,
                    "foreign_pre_chain": shared_processors,
                },
            },
            "loggers": {
                "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
                "uvicorn.error": {"handlers": ["default"], "level": "INFO", "propagate": False},
                "uvicorn.access": {"handlers": ["default"], "level": "INFO", "propagate": False},
                "": {"handlers": ["default"], "level": "INFO"},
            },
        }
    )
