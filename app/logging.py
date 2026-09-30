"""Structured application logging setup without sensitive payloads."""

import logging


def configure_logging(debug: bool) -> None:
    """Configure a single process-wide, conservative log format."""

    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
