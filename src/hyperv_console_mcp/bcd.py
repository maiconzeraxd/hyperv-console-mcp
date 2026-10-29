"""What a BCD value token means, decided once and on the host.

`bcdedit` prints the element names untranslated but everything around them in
the guest's display language, so a guest script may only report the token that
follows a name, never the answer. Two of them need the same reading of that
token — the guest configuration view and the lab diagnostics — and the reading
lives here so the two cannot drift apart.

Nothing here touches a VM: the input is a string a guest printed, which makes
the whole class of "the guest and the host disagree about a boot setting"
testable from Python.
"""

from __future__ import annotations

from typing import Any

# The values bcdedit prints, and the values it accepts as input. It writes
# 'Yes'/'No' on an English and a pt-BR guest alike, and takes 'on'/'off' in
# either; nothing else is a boolean it produced.
_ON = frozenset({"yes", "on", "true"})
_OFF = frozenset({"no", "off", "false"})


def bcd_boolean(token: Any) -> bool | None:
    """True or False for a BCD token, ``None`` when the token is not one of them.

    An absent element reads as False: bcdedit omits a setting that is off, so
    "not there" is Windows saying "off", not a missing measurement. An
    unrecognised token is ``None`` instead — a value this build cannot read is
    not evidence that the setting is disabled.
    """
    if token is None:
        return False
    value = str(token).strip().lower()
    if value in _ON:
        return True
    if value in _OFF:
        return False
    return None
