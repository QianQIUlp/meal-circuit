"""MealCircuit local-first application."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as installed_version
from pathlib import Path
import tomllib


try:
    __version__ = installed_version("mealcircuit")
except PackageNotFoundError:
    with (Path(__file__).resolve().parents[1] / "pyproject.toml").open("rb") as handle:
        __version__ = tomllib.load(handle)["project"]["version"]
