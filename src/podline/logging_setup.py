import logging
from pathlib import Path


def configure_logging(logs_dir: str, debug: bool = False) -> logging.Logger:
    Path(logs_dir).mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("podline")
    level = logging.DEBUG if debug else logging.INFO
    logger.setLevel(level)

    if logger.handlers:
        for handler in logger.handlers:
            handler.setLevel(level)
        return logger

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(Path(logs_dir) / "automation.log")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
