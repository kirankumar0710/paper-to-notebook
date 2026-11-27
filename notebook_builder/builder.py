"""Turn the model's notebook description into a Colab-ready ``.ipynb``."""

from __future__ import annotations

import re
import shlex
from typing import Any

from nbformat import NotebookNode
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

from util.log import get_logger

__all__ = [
    "QUICK_RUN_TAG",
    "build_notebook",
    "pip_install_source",
    "quick_run_source",
    "split_packages",
    "validate_spec",
]

logger = get_logger(__name__)

# Package specs such as "torch", "einops>=0.7", "jax[cuda12]". Anything
# else (spaces, quotes, ";", "&&") is dropped so the install cell cannot
# run arbitrary shell commands.
_PACKAGE_SPEC = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\],<>=!~*+]*$")

# Every notebook gets this switch near the top; the prompt tells the model
# to scale data and training down when it is True. The verify step runs
# the notebook with it set to True.
QUICK_RUN_TAG = "quick-run"


def quick_run_source(value: bool) -> str:
    """The source of the ``QUICK_RUN`` cell."""
    return (
        "# QUICK_RUN = True runs a fast smoke test (tiny data, few steps).\n"
        "# Leave it False to run the full experiment.\n"
        f"QUICK_RUN = {value}"
    )


DISCLAIMER = (
    "*Generated from the research paper by an LLM. "
    "Review the code before relying on its results.*"
)


def split_packages(packages: list[Any]) -> tuple[list[str], list[str]]:
    """De-duplicate pip specs and split them into ``(valid, dropped)``.

    Dropped specs are anything that is not a plain package spec (e.g. it
    contains spaces, quotes or shell operators).
    """
    unique = dict.fromkeys(str(p).strip() for p in packages)
    valid = [p for p in unique if _PACKAGE_SPEC.match(p)]
    dropped = [p for p in unique if p not in valid]
    return valid, dropped


def pip_install_source(packages: list[Any]) -> str | None:
    """The ``!pip install`` line for valid, de-duplicated packages."""
    valid, dropped = split_packages(packages)
    if dropped:
        logger.warning("Dropped unsafe pip package specs: %s", dropped)
    if not valid:
        return None
    return "!pip install -q " + " ".join(shlex.quote(p) for p in valid)


def validate_spec(spec: dict[str, Any]) -> None:
    """Check the parts of the model's reply that the builder relies on."""
    if not isinstance(spec.get("title"), str):
        raise ValueError("Notebook spec is missing a 'title' string.")
    cells = spec.get("cells")
    if not isinstance(cells, list) or not cells:
        raise ValueError("Notebook spec has no cells.")
    for index, cell in enumerate(cells):
        if not isinstance(cell, dict) or cell.get("cell_type") not in (
            "markdown",
            "code",
        ):
            raise ValueError(f"Cell {index} has an invalid 'cell_type'.")
        if not isinstance(cell.get("source"), str):
            raise ValueError(f"Cell {index} has no 'source' string.")


def build_notebook(
    spec: dict[str, Any],
    *,
    filename: str = "notebook.ipynb",
    note: str | None = None,
) -> NotebookNode:
    """Assemble a Colab notebook from a spec matching ``NOTEBOOK_SCHEMA``.

    Args:
        spec: The model's reply: title, pip packages, GPU flag and cells.
        filename: Name Colab shows for the notebook.
        note: Optional line for the title cell, e.g. the verify result.

    Returns:
        A notebook node that can be written to an ``.ipynb`` file.

    Raises:
        ValueError: If the spec is missing required fields.
    """
    validate_spec(spec)

    header = f"# {spec['title'].strip()}\n\n{DISCLAIMER}"
    if note:
        header += f"\n\n{note}"
    cells = [new_markdown_cell(header)]
    install = pip_install_source(spec.get("pip_packages") or [])
    if install is not None:
        cells.append(new_code_cell(install))
    cells.append(
        new_code_cell(
            quick_run_source(False), metadata={"tags": [QUICK_RUN_TAG]}
        )
    )
    for cell in spec["cells"]:
        source = cell["source"].strip("\n")
        if cell["cell_type"] == "code":
            cells.append(new_code_cell(source))
        else:
            cells.append(new_markdown_cell(source))

    notebook = new_notebook(cells=cells)
    notebook.metadata.update(
        {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python"},
            "colab": {"name": filename, "provenance": []},
        }
    )
    if spec.get("requires_gpu"):
        notebook.metadata["accelerator"] = "GPU"
        notebook.metadata["colab"]["gpuType"] = "T4"
    return notebook
