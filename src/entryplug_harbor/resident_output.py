"""Keep native process diagnostics off the resident's private JSON record pipe."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO


@contextmanager
def private_record_stream() -> Iterator[TextIO]:
    """Reserve stdout for records; redirect Python and native fd-1 diagnostics.

    Call only in the owned worker process, before native runtime initialization.
    A Python-only redirect does not catch messages written directly by ROS or
    native libraries. The duplicated record descriptor is not inherited by new
    worker processes.
    """
    sys.stdout.flush()
    descriptor = sys.stdout.fileno()
    with os.fdopen(os.dup(descriptor), "w", encoding="utf-8", buffering=1) as records:
        os.dup2(sys.stderr.fileno(), descriptor)
        try:
            yield records
        finally:
            sys.stdout.flush()
            os.dup2(records.fileno(), descriptor)
