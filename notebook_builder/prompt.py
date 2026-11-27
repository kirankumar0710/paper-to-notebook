"""Instructions and output schema for turning a paper into a notebook."""

from __future__ import annotations

from typing import Any, Literal

__all__ = [
    "DEFAULT_DEPTH",
    "DEPTHS",
    "Depth",
    "NOTEBOOK_SCHEMA",
    "SYSTEM_PROMPT",
    "USER_PROMPT",
    "system_prompt",
]

# Who the notebook is for: how much it explains compared with how much it
# implements. Only the reader paragraph of the system prompt changes, so
# depth works the same with every provider.
Depth = Literal["beginner", "practitioner", "expert"]
DEPTHS: tuple[Depth, ...] = ("beginner", "practitioner", "expert")
DEFAULT_DEPTH: Depth = "practitioner"

_READERS: dict[str, str] = {
    "beginner": """\
The reader knows Python and basic maths but is new to this field. Explain \
the background the paper assumes before using it: define every term and \
symbol, explain each equation term by term in plain words, and build \
intuition with tiny worked examples and plots before the full method. \
Keep each code cell short and comment it generously. Favour explanation \
over breadth: implement the core idea in its simplest form, and leave \
optional variants out.""",
    "practitioner": """\
The reader is a practitioner who wants to understand the paper's method \
well enough to reuse it. Build the notebook around the paper's core \
contribution rather than summarising every section.""",
    "expert": """\
The reader is a researcher in this field. Skip background and textbook \
material, and keep markdown brief: focus it on what is new, the key \
equations, and implementation choices the paper leaves open. Favour \
implementation over explanation: implement the method completely and \
faithfully, with clean, vectorised code, and include the paper's main \
variants or ablations where they fit the time budget.""",
}

_SYSTEM_TEMPLATE = """\
You turn research papers into Google Colab notebooks that teach the paper \
by implementing it.

{reader}

Write the notebook as an ordered list of markdown and code cells:
- Start with what problem the paper solves and what its key idea is.
- Explain each part of the method in markdown before the code that \
implements it. Write equations in LaTeX ($...$ inline, $$...$$ for display).
- Implement the core algorithm(s) in clear, runnable Python. Use small \
synthetic or built-in datasets so every cell runs on a free Colab runtime \
in a few minutes.
- Run the code cells top to bottom without errors: define everything \
before it is used, and do not rely on files that are not downloaded in \
the notebook.
- An earlier cell already defines `QUICK_RUN = False`; do not define it \
again. When `QUICK_RUN` is True, the notebook must finish in about a \
minute on a CPU: use a tiny slice of the data and very few epochs or \
iterations, e.g. `epochs = 1 if QUICK_RUN else 20`. When it is False, run \
the full experiment.
- Where it helps understanding, plot results with matplotlib.
- When the notebook's numbers cannot match the paper (smaller data, \
fewer steps), say so plainly rather than implying the paper's results \
were reproduced.
- End with a short summary and ideas for extending the experiment.

Do not put `pip install` commands in code cells. Instead, list in \
`pip_packages` only the packages that are not already installed on Google \
Colab. Set `requires_gpu` to true only when the code needs a GPU to finish \
in reasonable time.
"""


def system_prompt(depth: str = DEFAULT_DEPTH) -> str:
    """The system prompt for a notebook written for ``depth``.

    Raises:
        ValueError: If ``depth`` is not one of :data:`DEPTHS`.
    """
    if depth not in _READERS:
        raise ValueError(
            f"Unknown depth {depth!r}; use one of: {', '.join(DEPTHS)}."
        )
    return _SYSTEM_TEMPLATE.format(reader=_READERS[depth])


SYSTEM_PROMPT = system_prompt(DEFAULT_DEPTH)

USER_PROMPT = (
    "Create a Google Colab notebook that implements and explains the "
    "attached research paper."
)

NOTEBOOK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "The paper's title.",
        },
        "pip_packages": {
            "type": "array",
            "items": {"type": "string"},
            "description": "PyPI packages not preinstalled on Colab.",
        },
        "requires_gpu": {"type": "boolean"},
        "cells": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cell_type": {
                        "type": "string",
                        "enum": ["markdown", "code"],
                    },
                    "source": {"type": "string"},
                },
                "required": ["cell_type", "source"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "pip_packages", "requires_gpu", "cells"],
    "additionalProperties": False,
}
