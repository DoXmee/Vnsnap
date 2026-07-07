from __future__ import annotations

import logging
import sys


try:
    from loguru import logger as logger
except Exception:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )
    logger = logging.getLogger("fast_tts")

