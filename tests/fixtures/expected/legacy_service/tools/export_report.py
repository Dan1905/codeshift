"""Legacy nightly export script. Not imported by the service."""

import subprocess
import time
from collections import OrderedDict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor


def flatten(record, prefix=""):
    out = OrderedDict()
    for key, value in record.items():
        if isinstance(value, Mapping):
            out.update(flatten(value, prefix + key + "."))
        else:
            out[prefix + key] = value
    return out


def read_rows(path):
    try:
        with open(path) as handle:
            return handle.read().splitlines()
    except OSError:
        return []


def throttle(row):
    time.sleep(0.01)
    return row


def export(path):
    with ThreadPoolExecutor() as pool:
        return list(pool.map(throttle, read_rows(path)))


def archive(path):
    time.sleep(1)
    try:
        subprocess.run(["gzip", path], check=True)
    except:
        return False
    return True
