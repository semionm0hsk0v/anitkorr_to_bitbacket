from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from functools import lru_cache
from logging.handlers import (
    RotatingFileHandler,
    TimedRotatingFileHandler,
)
from pathlib import Path

import structlog

from src.common.config.config import settings

PIPELINE_LOGGER_PREFIXES = ("src.agents", "src.app.mcp", "src.infrastructure.prompts")


def _parse_level(
    level_name: str,
    default: int = logging.INFO,
) -> int:
    name = (level_name or "").strip().upper()
    return getattr(logging, name, default)


@dataclass(frozen=True)
class LoggerConfig:
    module_name: str
    level: int
    renderer: str  # "json" | "console"

    # stream split
    split_stdout_stderr: bool
    stderr_from_level: int  # WARNING by default

    # file logging
    enable_file_log: bool
    log_dir: Path
    app_log_name: str
    error_log_name: str
    separate_error_log: bool

    # rotation
    rotation: str  # "size" | "time"
    max_bytes: int
    backup_count: int
    when: str
    interval: int
    utc: bool

    # behavior
    force_reconfigure: bool
    callsite: bool
    llm_only: bool

    # json logs: None = one line (NDJSON); positive int = json.dumps(indent=…)
    json_indent: int | None


def _parse_json_indent(raw: object) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        n = raw
    else:
        s = str(raw).strip()
        if not s:
            return None
        try:
            n = int(s, 10)
        except ValueError:
            return None
    if n <= 0:
        return None
    return n


def get_config() -> LoggerConfig:
    module_name: str = settings["MODULE_NAME"]

    level: int = _parse_level(
        settings["LOGLEVEL"],
        default=logging.INFO,
    )
    renderer: str = settings["LOG_RENDERER"].lower()
    if renderer not in {
        "json",
        "console",
    }:
        renderer = "json"

    split_stdout_stderr: bool = settings["LOG_SPLIT_STDERR"]
    stderr_from_level: int = _parse_level(
        settings["LOG_STDERR_FROM"],
        default=logging.WARNING,
    )

    enable_file_log: bool = settings["ENABLE_FILE_LOG"]
    log_dir = Path(settings["LOG_DIR"])
    app_log_name: str = settings["APP_LOG_NAME"]
    error_log_name: str = settings["ERROR_LOG_NAME"]
    separate_error_log: bool = settings["SEPARATE_ERROR_LOG"]

    rotation: str = settings["LOG_ROTATION"].lower()  # size | time
    if rotation not in {"size", "time"}:
        rotation = "size"

    max_bytes: int = settings["LOG_MAX_BYTES"]
    backup_count: int = settings["LOG_BACKUP_COUNT"]

    # timed rotation
    when: str = settings["LOG_WHEN"]  # e.g. midnight, H, D, W0...
    interval: int = settings["LOG_INTERVAL"]
    utc: bool = settings["LOG_UTC"]

    force_reconfigure: bool = settings["FORCE_LOGGING_CONFIG"]
    callsite: bool = settings["LOG_CALLSITE"]
    llm_only: bool = settings["LOG_LLM_ONLY"]

    json_indent: int | None = _parse_json_indent(settings.get("LOG_JSON_INDENT"))

    return LoggerConfig(
        module_name=module_name,
        level=level,
        renderer=renderer,
        split_stdout_stderr=split_stdout_stderr,
        stderr_from_level=stderr_from_level,
        enable_file_log=enable_file_log,
        log_dir=log_dir,
        app_log_name=app_log_name,
        error_log_name=error_log_name,
        separate_error_log=separate_error_log,
        rotation=rotation,
        max_bytes=max_bytes,
        backup_count=backup_count,
        when=when,
        interval=interval,
        utc=utc,
        force_reconfigure=force_reconfigure,
        callsite=callsite,
        llm_only=llm_only,
        json_indent=json_indent,
    )


class MaxLevelFilter(logging.Filter):
    """Пропускает записи с level < max_level (строго)."""

    def __init__(self, max_level: int) -> None:
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self.max_level


class MinLevelFilter(logging.Filter):
    """Пропускает записи с level >= min_level (включительно)."""

    def __init__(self, min_level: int) -> None:
        super().__init__()
        self.min_level = min_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= self.min_level


class PipelineOnlyFilter(logging.Filter):
    """Пропускает только записи боевых маршрутов (``PIPELINE_LOGGER_PREFIXES``)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return any(record.name == prefix or record.name.startswith(prefix + ".") for prefix in PIPELINE_LOGGER_PREFIXES)


def _attach_handler_filters(handler: logging.Handler, cfg: LoggerConfig) -> None:
    if cfg.llm_only:
        handler.addFilter(PipelineOnlyFilter())


def _reset_root_logger(root: logging.Logger, level: int) -> None:
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)


def _make_processor_formatter(
    cfg: LoggerConfig,
) -> logging.Formatter:
    if cfg.renderer == "json":
        indent_kw = {"indent": cfg.json_indent} if cfg.json_indent is not None else {}
        renderer: structlog.processors.JSONRenderer | structlog.dev.ConsoleRenderer = structlog.processors.JSONRenderer(
            serializer=lambda obj, **kw: json.dumps(obj, ensure_ascii=False, **kw),
            **indent_kw,
        )
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    pre_chain: list[structlog.typing.Processor] = [
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.stdlib.add_logger_name,
    ]

    return structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=pre_chain,
    )


def _configure_stream_handlers(
    root: logging.Logger,
    formatter: logging.Formatter,
    cfg: LoggerConfig,
) -> None:
    if not cfg.split_stdout_stderr:
        h = logging.StreamHandler(stream=sys.stdout)
        h.setLevel(cfg.level)
        h.setFormatter(formatter)
        _attach_handler_filters(h, cfg)
        root.addHandler(h)
        return

    h_out = logging.StreamHandler(stream=sys.stdout)
    h_out.setLevel(cfg.level)
    h_out.addFilter(MaxLevelFilter(cfg.stderr_from_level))
    h_out.setFormatter(formatter)
    _attach_handler_filters(h_out, cfg)

    h_err = logging.StreamHandler(stream=sys.stderr)
    h_err.setLevel(cfg.level)
    h_err.addFilter(MinLevelFilter(cfg.stderr_from_level))
    h_err.setFormatter(formatter)
    _attach_handler_filters(h_err, cfg)

    root.addHandler(h_out)
    root.addHandler(h_err)


def _make_file_handler(cfg: LoggerConfig, path: Path) -> logging.Handler:
    if cfg.rotation == "time":
        return TimedRotatingFileHandler(
            filename=str(path),
            when=cfg.when,
            interval=cfg.interval,
            backupCount=cfg.backup_count,
            utc=cfg.utc,
            encoding="utf-8",
        )
    return RotatingFileHandler(
        filename=str(path),
        maxBytes=cfg.max_bytes,
        backupCount=cfg.backup_count,
        encoding="utf-8",
    )


def _configure_file_handlers(
    root: logging.Logger,
    formatter: logging.Formatter,
    cfg: LoggerConfig,
) -> None:
    if not cfg.enable_file_log:
        return

    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    app_path = cfg.log_dir / cfg.app_log_name
    err_path = cfg.log_dir / cfg.error_log_name

    # app.log: если включён отдельный error.log — не дублируем ERROR+ в app.log
    h_app = _make_file_handler(cfg, app_path)
    h_app.setLevel(cfg.level)
    if cfg.separate_error_log:
        h_app.addFilter(MaxLevelFilter(logging.ERROR))
    h_app.setFormatter(formatter)
    _attach_handler_filters(h_app, cfg)
    root.addHandler(h_app)

    # error.log: только ERROR+
    if cfg.separate_error_log:
        h_err = _make_file_handler(cfg, err_path)
        h_err.setLevel(logging.ERROR)
        h_err.addFilter(MinLevelFilter(logging.ERROR))
        h_err.setFormatter(formatter)
        _attach_handler_filters(h_err, cfg)
        root.addHandler(h_err)


def _configure_structlog(
    cfg: LoggerConfig,
) -> None:
    processors: list[structlog.typing.Processor] = [
        structlog.stdlib.filter_by_level,
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.stdlib.add_logger_name,
    ]

    if cfg.callsite:
        processors.append(
            structlog.processors.CallsiteParameterAdder(
                {
                    structlog.processors.CallsiteParameter.FILENAME,
                    structlog.processors.CallsiteParameter.LINENO,
                    structlog.processors.CallsiteParameter.FUNC_NAME,
                }
            )
        )

    processors.extend(
        [
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ]
    )

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


@lru_cache(maxsize=1)
def _configure_once(
    cfg: LoggerConfig,
) -> None:
    root = logging.getLogger()
    _reset_root_logger(root, cfg.level)

    formatter = _make_processor_formatter(cfg)

    _configure_stream_handlers(root, formatter, cfg)
    _configure_file_handlers(root, formatter, cfg)
    _configure_structlog(cfg)
    _quiet_third_party_loggers()


def _quiet_third_party_loggers() -> None:
    for name in (
        "httpx",
        "httpcore",
        "httpcore.http11",
        "httpcore.connection",
        "urllib3",
        "openai._base_client",
        "opensearch",
        "opensearchpy",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)


def configure_logging(
    cfg: LoggerConfig | None = None,
) -> None:
    "Configure stdlib logging + structlog formatting."
    if cfg is None:
        cfg = get_config()

    if cfg.force_reconfigure:
        _configure_once.cache_clear()

    _configure_once(cfg)


def get_logger(
    name: str | None = None,
) -> structlog.stdlib.BoundLogger:
    cfg = get_config()
    return structlog.get_logger(name or cfg.module_name)
