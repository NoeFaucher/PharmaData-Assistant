"""Centralised logging setup for the ParmaTech assistant.

Usage in any module:
    from logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("something happened")

Log levels:
    DEBUG   — tool inputs/outputs, raw LLM messages
    INFO    — high-level flow (tool called, node entered, chat turn)
    WARNING — recoverable issues (unknown operation, missing result var)
    ERROR   — exceptions caught inside tools

Environment variables:
    LOG_LEVEL       — console level (default: INFO)
    LOG_LEVEL_FILE  — file level    (default: DEBUG)
    LOG_FILE        — log file path (default: logs/pharmatech.log)
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False

CONSOLE_FMT = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
FILE_FMT    = "%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s"
DATE_FMT    = "%H:%M:%S"


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # handlers filter individually

    # --- console handler ---
    console_level = os.getenv("LOG_LEVEL", "INFO").upper()
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(getattr(logging, console_level, logging.INFO))
    ch.setFormatter(logging.Formatter(CONSOLE_FMT, datefmt=DATE_FMT))
    root.addHandler(ch)

    # --- rotating file handler ---
    log_file = os.getenv("LOG_FILE", os.path.join("./", "logs", "pharmatech.log"))
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    file_level = os.getenv("LOG_LEVEL_FILE", "DEBUG").upper()
    fh = RotatingFileHandler(log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setLevel(getattr(logging, file_level, logging.DEBUG))
    fh.setFormatter(logging.Formatter(FILE_FMT, datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(fh)

    # Silence noisy third-party loggers
    for noisy in ("httpx", "httpcore", "openrouter", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
