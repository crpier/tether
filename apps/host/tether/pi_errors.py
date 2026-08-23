"""Error identities for the host-owned pi runtime boundary."""


class PiRuntimeError(Exception):
    """Failure while speaking to or managing a pi RPC subprocess."""


class PiPreacceptTransientError(PiRuntimeError):
    """Known transient transport failure proven to precede prompt acceptance."""


__all__ = ["PiPreacceptTransientError", "PiRuntimeError"]
