"""Behavior tests for worktree-safe developer tooling."""

import os
import subprocess
import tomllib
from pathlib import Path

from snektest import assert_eq, assert_true, test

REPO_ROOT = Path(__file__).resolve().parents[3]


@test()
def just_pins_host_uv_environment_to_the_current_checkout() -> None:
    env = dict(os.environ)
    env["UV_PROJECT"] = "/not-this-checkout/apps/host"
    env["VIRTUAL_ENV"] = "/not-this-checkout/apps/host/.venv"

    uv_project = subprocess.run(
        ["just", "--evaluate", "UV_PROJECT"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    virtual_env = subprocess.run(
        ["just", "--evaluate", "VIRTUAL_ENV"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert_eq(uv_project.stdout.strip(), str(REPO_ROOT / "apps/host"))
    assert_eq(virtual_env.stdout.strip(), str(REPO_ROOT / "apps/host/.venv"))


@test()
def snektest_source_does_not_escape_the_checkout_by_relative_path() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "apps/host/pyproject.toml").read_text())
    source = pyproject["tool"]["uv"]["sources"]["snektest"]
    source_path = source.get("path")

    assert_true(
        source_path is None
        or Path(source_path).is_absolute()
        or ".." not in Path(source_path).parts
    )
