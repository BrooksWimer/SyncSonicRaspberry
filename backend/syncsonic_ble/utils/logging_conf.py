"""Central logging setup so *every* module shares the same formatter."""
import logging
import os
import sys

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
LOG_LEVEL = os.environ.get("SYNCSONIC_LOG_LEVEL", "INFO").strip().upper()
_LEVEL = getattr(logging, LOG_LEVEL, logging.INFO)

root_logger = logging.getLogger()
if not root_logger.handlers:
    logging.basicConfig(
        level=_LEVEL,
        format=LOG_FORMAT,
        handlers=[logging.StreamHandler(sys.stdout)],
    )
else:
    root_logger.setLevel(_LEVEL)

# Keep chatty third-party logs from flooding journal output.
for _name in ("matplotlib", "PIL", "urllib3"):
    logging.getLogger(_name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
