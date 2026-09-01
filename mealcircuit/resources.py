from __future__ import annotations

import sys
import sysconfig
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=None)
def resource_roots(category: str) -> tuple[Path, ...]:
    """Return source, frozen-app, and wheel data roots for one resource group."""
    if not category or Path(category).name != category:
        raise ValueError(f"invalid resource category: {category!r}")

    package_parent = Path(__file__).resolve().parent.parent
    roots = [package_parent / category]

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        roots.append(Path(frozen_root) / category)

    # ``pip install --target`` places data files alongside the package under
    # ``share``. A regular virtual-environment install uses sysconfig's data
    # prefix instead.
    roots.append(package_parent / "share" / "mealcircuit" / category)
    data_root = sysconfig.get_path("data")
    if data_root:
        roots.append(Path(data_root) / "share" / "mealcircuit" / category)
    return tuple(dict.fromkeys(roots))


def resource_path(category: str, name: str) -> Path:
    """Resolve a named bundled resource, retaining a useful missing-file path."""
    if not name or Path(name).name != name:
        raise ValueError(f"invalid resource name: {name!r}")
    roots = resource_roots(category)
    for root in roots:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return roots[0] / name
