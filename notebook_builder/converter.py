"""Convert a research paper PDF into a Colab notebook with an LLM."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath

from nbformat import NotebookNode

from notebook_builder.builder import build_notebook
from notebook_builder.prompt import NOTEBOOK_SCHEMA, SYSTEM_PROMPT, USER_PROMPT
from providers.base import LLMProvider, Progress, ProgressCallback
from util.log import get_logger
from util.text_utils import sanitize_filename

__all__ = ["ConvertedNotebook", "pdf_to_notebook"]

MAX_FILENAME_CHARS = 50

logger = get_logger(__name__)


@dataclass(frozen=True)
class ConvertedNotebook:
    """A generated notebook and the filename to save it under."""

    title: str
    filename: str
    notebook: NotebookNode


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

    Returns:
        The notebook and its filename.

    Raises:
        ProviderError: If the LLM request fails.
        ValueError: If the model's reply is not a usable notebook.
    """
    spec = provider.generate_json(
        USER_PROMPT,
        schema=NOTEBOOK_SCHEMA,
        system=SYSTEM_PROMPT,
        pdf=pdf,
        on_progress=on_progress,
    )
    if on_progress is not None:
        on_progress(Progress("building", "Building the notebook…"))
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
    filename = _notebook_filename(source_name, title)
    notebook = build_notebook(spec, filename=filename)
    logger.info(
        "Built notebook %r with %d cells", filename, len(notebook.cells)
    )
    return ConvertedNotebook(title=title, filename=filename, notebook=notebook)
