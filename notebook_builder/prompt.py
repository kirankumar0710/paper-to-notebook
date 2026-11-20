"""Instructions and output schema for turning a paper into a notebook."""

from __future__ import annotations

from typing import Any

__all__ = ["NOTEBOOK_SCHEMA", "SYSTEM_PROMPT", "USER_PROMPT"]

SYSTEM_PROMPT = """\
You turn research papers into Google Colab notebooks that teach the paper \
by implementing it.

The reader is a practitioner who wants to understand the paper's method \
well enough to reuse it. Build the notebook around the paper's core \
contribution rather than summarising every section.

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
