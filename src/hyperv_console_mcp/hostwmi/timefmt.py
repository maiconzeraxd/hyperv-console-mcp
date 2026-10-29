"""One timestamp format, because two callers have to agree on it.

The bridge writes a ``DateTime`` with ``ToString('o')`` — seven fractional
digits and the offset of the host's zone — and every operation that answers
with a time is compared against that. The extra digit is the reason this is a
function and not a ``strftime`` call: ``%f`` gives six.
"""

from __future__ import annotations

from datetime import datetime


def local_iso(moment: datetime, tick: int = 0) -> str:
    """The bridge's round-trip format for a moment already in local time.

    ``tick`` is the seventh digit — .NET's 100 ns unit — which ``datetime`` has
    no field for. Only the event log carries a non-zero one; a CIM DATETIME
    stops at six digits, and there the default is the whole truth.
    """
    zone = moment.strftime("%z")
    return (
        f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{moment.microsecond:06d}{tick}"
        f"{zone[:3]}:{zone[3:]}"
    )
