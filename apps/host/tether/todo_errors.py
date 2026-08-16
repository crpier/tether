"""Stable error identities for the Todo vertical."""


class TodoNotFoundError(Exception):
    """An operation targeted a Todo that does not exist."""


class TodoConflictError(Exception):
    """A Todo moved on from the version on which a caller acted."""


class InvalidTodoError(Exception):
    """A Todo's action text is blank after trimming."""
