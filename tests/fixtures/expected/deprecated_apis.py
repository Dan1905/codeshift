"""Fixture: deprecated standard-library APIs mixed with look-alikes that must not change."""

import datetime as dt
import logging
from collections import OrderedDict
from collections.abc import Mapping
from datetime import timezone, datetime

logger = logging.getLogger(__name__)  # module logger


class Alarm:
    def warn(self, message):
        return f"alarm: {message}"

    def utcnow(self):
        return "not a datetime"


def stamp(seconds):
    created = datetime.now(timezone.utc)  # naive UTC
    also = dt.datetime.now(dt.timezone.utc)
    parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return created, also, parsed


def report(alarm, payload):
    if isinstance(payload, Mapping):
        logger.warning("payload has %d keys", len(payload))
    logging.warning("legacy call")
    alarm.warn("unrelated method")  # must stay
    return OrderedDict(payload), alarm.utcnow()


def load(path):
    try:
        with open(path) as handle:
            return handle.read()
    except OSError as exc:
        raise OSError(f"cannot read {path}") from exc
