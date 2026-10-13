"""Fixture: deprecated standard-library APIs mixed with look-alikes that must not change."""

import datetime as dt
import logging
from collections import Mapping, OrderedDict
from datetime import datetime

logger = logging.getLogger(__name__)  # module logger


class Alarm:
    def warn(self, message):
        return f"alarm: {message}"

    def utcnow(self):
        return "not a datetime"


def stamp(seconds):
    created = datetime.utcnow()  # naive UTC
    also = dt.datetime.utcnow()
    parsed = datetime.utcfromtimestamp(seconds)
    return created, also, parsed


def report(alarm, payload):
    if isinstance(payload, Mapping):
        logger.warn("payload has %d keys", len(payload))
    logging.warn("legacy call")
    alarm.warn("unrelated method")  # must stay
    return OrderedDict(payload), alarm.utcnow()


def load(path):
    try:
        with open(path) as handle:
            return handle.read()
    except IOError as exc:
        raise EnvironmentError(f"cannot read {path}") from exc
