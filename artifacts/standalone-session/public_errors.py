"""Refusals whose messages are authored here and are safe to show in the local UI.

The web middleware returns ``str(error)`` only for these classes. Every other
exception still becomes a generic message, so vendor responses, identifiers and
URLs never reach the browser. Rule: construct these with a string literal only.
``test_public_errors.py`` enforces that rule across the package.

Each class also derives from the builtin the code raised before, so existing
callers and tests that catch ValueError/PermissionError/... keep working.
"""


class PublicError(Exception):
    code='refused'


class Refused(PublicError,ValueError):
    """The request was refused: invalid input (including a non-JSON body) or a guard."""
    code='refused'


class NotReady(PublicError,PermissionError):
    """The owner has not confirmed physical readiness (or it expired)."""
    code='not_ready'


class Unavailable(PublicError,ConnectionError):
    """A required connection, session or optional component is missing."""
    code='unavailable'


class Busy(PublicError,RuntimeError):
    """Another exclusive operation currently owns the resource."""
    code='busy'


class OutcomeUnknown(PublicError,RuntimeError):
    """A write's result is not known: no reply, an unusable reply, a readback that does
    not match, or an earlier uncertain write that a read could not settle. Never retry
    the write automatically; it may already have been applied."""
    code='outcome_unknown'


class CloudRejected(PublicError,RuntimeError):
    """The vendor cloud (or the custom assistant provider) answered but did not accept the request."""
    code='cloud_rejected'


PUBLIC_CLASSES=(Refused,NotReady,Unavailable,Busy,OutcomeUnknown,CloudRejected)

GENERIC_MESSAGE='Operation was not completed. Check readiness, session and private diagnostics.'


def describe(error):
    """JSON body for a failed request. Only PublicError text is ever echoed."""
    if isinstance(error,PublicError):
        return {'error':type(error).__name__,'code':error.code,'message':str(error)}
    return {'error':type(error).__name__,'code':'failed','message':GENERIC_MESSAGE}
