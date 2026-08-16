"""Error identities for the host-owned pi runtime boundary."""


class PiRuntimeError(Exception):
    """Failure while speaking to or managing a pi RPC subprocess."""


__all__ = ["PiRuntimeError"]
