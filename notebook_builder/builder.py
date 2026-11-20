"""Turn the model's notebook description into a Colab-ready ``.ipynb``."""

from __future__ import annotations

import re
import shlex
from typing import Any

from nbformat import NotebookNode
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

from util.log import get_logger

__all__ = ["build_notebook"]

logger = get_logger(__name__)

# Package specs such as "torch", "einops>=0.7", "jax[cuda12]". Anything
# else (spaces, quotes, ";", "&&") is dropped so the install cell cannot
# run arbitrary shell commands.
_PACKAGE_SPEC = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\],<>=!~*+]*$")

DISCLAIMER = (
    "*Generated from the research paper by an LLM. "
    "Review the code before relying on its results.*"
)


def _pip_install_cell(packages: list[str]) -> NotebookNode | None:
    """Build a ``!pip install`` cell for valid, de-duplicated packages."""
    unique = dict.fromkeys(p.strip() for p in packages)
    valid = [p for p in unique if _PACKAGE_SPEC.match(p)]
    dropped = [p for p in unique if p not in valid]
    if dropped:
        logger.warning("Dropped unsafe pip package specs: %s", dropped)
    if not valid:
        return None
    args = " ".join(shlex.quote(p) for p in valid)
    return new_code_cell(f"!pip install -q {args}")


def _validate(spec: dict[str, Any]) -> None:
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
    spec: dict[str, Any], *, filename: str = "notebook.ipynb"
) -> NotebookNode:
    """Assemble a Colab notebook from a spec matching ``NOTEBOOK_SCHEMA``.

    Args:
        spec: The model's reply: title, pip packages, GPU flag and cells.
        filename: Name Colab shows for the notebook.

    Returns:
        A notebook node that can be written to an ``.ipynb`` file.

    Raises:
        ValueError: If the spec is missing required fields.
    """
    _validate(spec)

    cells = [new_markdown_cell(f"# {spec['title'].strip()}\n\n{DISCLAIMER}")]
    install_cell = _pip_install_cell(spec.get("pip_packages") or [])
    if install_cell is not None:
        cells.append(install_cell)
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
