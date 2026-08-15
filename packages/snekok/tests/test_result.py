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
def ok_map_transforms_the_success_value() -> None:
    """`map` applies its transform to an `Ok` value."""
    assert_eq(Ok(2).map(lambda number: number * 3), Ok(6))


@test(mark="fast")
def err_map_preserves_the_error() -> None:
    """`map` leaves an `Err` unchanged."""
    assert_eq(Err("invalid").map(lambda _: "unused"), Err("invalid"))


@test(mark="fast")
def ok_map_error_preserves_the_success() -> None:
    """`map_error` leaves an `Ok` unchanged."""
    assert_eq(Ok(2).map_error(lambda _: "unused"), Ok(2))


@test(mark="fast")
def err_map_error_transforms_the_error() -> None:
    """`map_error` applies its transform to an `Err` value."""
    assert_eq(Err("invalid").map_error(str.upper), Err("INVALID"))


@test(mark="fast")
def ok_and_then_continues_with_the_next_result() -> None:
    """`and_then` applies a fallible continuation to an `Ok` value."""
    assert_eq(Ok(2).and_then(lambda number: Ok(number * 3)), Ok(6))


@test(mark="fast")
def err_and_then_preserves_the_error() -> None:
    """`and_then` does not run its continuation for an `Err` value."""
    assert_eq(Err("invalid").and_then(lambda _: Ok("unused")), Err("invalid"))


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
