"""Behavior tests for snekok's public Result variants."""

from dataclasses import FrozenInstanceError

from snektest import assert_eq, assert_false, assert_raises, fail, test

from snekok import Err, Ok, Result


def _success() -> Result[int, str]:
    """Return a union-typed success so both match branches remain possible."""
    return Ok(42)


def _failure() -> Result[int, str]:
    """Return a union-typed failure so both match branches remain possible."""
    return Err("invalid input")


@test(mark="fast")
def ok_exposes_its_value_through_structural_matching() -> None:
    """An `Ok` can be matched positionally to consume its success value."""
    match _success():
        case Ok(value):
            assert_eq(value, 42)
        case Err(error):
            fail(f"unexpected error: {error}")


@test(mark="fast")
def err_exposes_its_error_through_structural_matching() -> None:
    """An `Err` can be matched positionally to consume its failure value."""
    match _failure():
        case Ok(value):
            fail(f"unexpected value: {value}")
        case Err(error):
            assert_eq(error, "invalid input")


@test(mark="fast")
def ok_is_immutable() -> None:
    """An `Ok` value cannot change after construction."""
    success = Ok(42)

    with assert_raises(FrozenInstanceError):
        success.__setattr__("value", 43)


@test(mark="fast")
def err_is_immutable() -> None:
    """An `Err` value cannot change after construction."""
    failure = Err("invalid input")

    with assert_raises(FrozenInstanceError):
        failure.__setattr__("error", "different error")


@test(mark="fast")
def ok_has_no_instance_dictionary() -> None:
    """An `Ok` stores only its declared value field."""
    assert_false(hasattr(Ok(42), "__dict__"))


@test(mark="fast")
def err_has_no_instance_dictionary() -> None:
    """An `Err` stores only its declared error field."""
    assert_false(hasattr(Err("invalid input"), "__dict__"))
