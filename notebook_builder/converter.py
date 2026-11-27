"""Convert a research paper PDF into a Colab notebook with an LLM."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath

from nbformat import NotebookNode

from notebook_builder.builder import (
    build_notebook,
    split_packages,
    validate_spec,
)
from notebook_builder.prompt import (
    DEFAULT_DEPTH,
    NOTEBOOK_SCHEMA,
    USER_PROMPT,
    system_prompt,
)
from notebook_builder.verify import (
    VerifyReport,
    VerifySettings,
    verify_notebook,
)
from providers.base import LLMProvider, Progress, ProgressCallback
from util.log import get_logger
from util.text_utils import sanitize_filename

__all__ = ["ConvertedNotebook", "pdf_to_notebook"]

MAX_FILENAME_CHARS = 50

logger = get_logger(__name__)


@dataclass(frozen=True)
class ConvertedNotebook:
    """A generated notebook, the filename to save it under, and facts about
    its contents for the dashboard.

    Attributes:
        pip_packages: Extra packages installed by the notebook.
        dropped_packages: Package specs the model asked for that were
            removed because they were not plain package names.
        requires_gpu: Whether the notebook asks Colab for a GPU.
        code_lines: Non-blank lines in the model's code cells.
        verification: The result of the optional run check, or None if
            it was not requested.
        depth: Who the notebook was written for: ``beginner``,
            ``practitioner`` or ``expert``.
    """

    title: str
    filename: str
    notebook: NotebookNode
    pip_packages: tuple[str, ...] = ()
    dropped_packages: tuple[str, ...] = ()
    requires_gpu: bool = False
    code_lines: int = 0
    verification: VerifyReport | None = None
    depth: str = DEFAULT_DEPTH


def _notebook_filename(source_name: str | None, title: str) -> str:
    """Name the notebook after the uploaded PDF, else after the title."""
    for candidate in (PurePath(source_name or "").stem, title):
        filename = sanitize_filename(
            candidate, ".ipynb", max_length=MAX_FILENAME_CHARS, default=""
        )
        if filename != ".ipynb":
            return filename
    return "notebook.ipynb"


def pdf_to_notebook(
    pdf: bytes,
    provider: LLMProvider,
    *,
    source_name: str | None = None,
    on_progress: ProgressCallback | None = None,
    verify: VerifySettings | None = None,
    depth: str = DEFAULT_DEPTH,
) -> ConvertedNotebook:
    """Have ``provider`` read the paper and build a Colab notebook from it.

    Args:
        pdf: The paper's PDF bytes.
        provider: The LLM to use.
        source_name: The uploaded file's name, e.g. ``"my_paper.pdf"``.
            The notebook is named after it (``"my_paper.ipynb"``); without
            it, the paper's title is used.
        on_progress: Called with :class:`~providers.base.Progress` updates
            while the model works and the notebook is built.
        verify: Run the notebook and let the model fix failing cells
            (see :mod:`notebook_builder.verify`). None skips the check.
        depth: Who the notebook is for (``beginner``, ``practitioner`` or
            ``expert``): how much it explains compared with how much it
            implements. Changes only the system prompt.

    Returns:
        The notebook and its filename.

    Raises:
        ProviderError: If the LLM request fails.
        ValueError: If ``depth`` is unknown, or the model's reply is not a
            usable notebook.
    """
    system = system_prompt(depth)  # before any paid request
    spec = provider.generate_json(
        USER_PROMPT,
        schema=NOTEBOOK_SCHEMA,
        system=system,
        pdf=pdf,
        on_progress=on_progress,
    )
    title = str(spec.get("title", "")).strip()
    cells = spec.get("cells") or []
    kinds = [c.get("cell_type") for c in cells if isinstance(c, dict)]
    logger.info(
        "Model returned: title=%r, %d cells (%d markdown, %d code), "
        "pip_packages=%s, requires_gpu=%s",
        title,
        len(cells),
        kinds.count("markdown"),
        kinds.count("code"),
        spec.get("pip_packages"),
        spec.get("requires_gpu"),
    )
    report: VerifyReport | None = None
    if verify is not None:
        validate_spec(spec)  # before running any of it
        spec, report = verify_notebook(
            spec, provider, verify, on_progress=on_progress
        )
        cells = spec.get("cells") or []
    if on_progress is not None:
        on_progress(Progress("building", "Building the notebook…"))
    filename = _notebook_filename(source_name, title)
    notebook = build_notebook(
        spec, filename=filename, note=report.note() if report else None
    )
    notebook.metadata["paper_to_notebook"] = {"depth": depth}
    logger.info(
        "Built notebook %r with %d cells", filename, len(notebook.cells)
    )
    packages, dropped = split_packages(spec.get("pip_packages") or [])
    code_lines = sum(
        1
        for cell in cells
        if isinstance(cell, dict) and cell.get("cell_type") == "code"
        for line in str(cell.get("source", "")).splitlines()
        if line.strip()
    )
    return ConvertedNotebook(
        title=title,
        filename=filename,
        notebook=notebook,
        pip_packages=tuple(packages),
        dropped_packages=tuple(dropped),
        requires_gpu=bool(spec.get("requires_gpu")),
        code_lines=code_lines,
        verification=report,
        depth=depth,
    )
