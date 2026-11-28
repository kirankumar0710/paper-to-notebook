"""Run a notebook's code cells in order and report each result.

Started by :mod:`notebook_builder.verify` with the sandbox interpreter, not
imported by the app, so it uses only the standard library. It reads
``{"cells": [source, ...]}`` as JSON on stdin and writes one protocol line
per event on stdout, each starting with ``PREFIX`` so output from the
notebook's own subprocesses can be told apart:

- ``{"event": "start", "cell": i}`` before cell ``i`` runs
- ``{"event": "ok", "cell": i, "output": ...}`` when it finishes
- ``{"event": "error", "cell": i, "error": ..., "traceback": ...,
  "output": ...}`` when it raises; nothing more runs after that
- ``{"event": "finished"}`` after the last cell

Like Colab, ``!command`` lines run in a shell, and ``%magic`` lines are
skipped.
"""

import contextlib
import io
import json
import re
import subprocess
import sys
import traceback

# Keep in sync with notebook_builder.verify.PROTOCOL_PREFIX.
PREFIX = "@@p2n-verify@@ "
MAX_TEXT = 4000
MAX_OUTPUT = 2000  # of a passing cell's output

_SHELL_LINE = re.compile(r"^(\s*)!(.*)$")
_MAGIC_LINE = re.compile(r"^(\s*)%")


def send(stream, **message):
    """Write one protocol line."""
    stream.write(PREFIX + json.dumps(message) + "\n")
    stream.flush()


def shell(command):
    """Run a notebook ``!command`` line as Colab does: show its output and
    carry on if it fails."""
    # The notebook's own shell lines, run inside the sandbox.
    result = subprocess.run(  # noqa: S602
        command,
        # nosemgrep: subprocess-shell-true
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def display(*objects, **_kwargs):
    """Stand-in for IPython's ``display()``."""
    for obj in objects:
        print(repr(obj))


def to_python(source):
    """Turn notebook-only syntax (``!cmd``, ``%magic``) into plain Python."""
    lines = []
    for line in source.splitlines():
        shell_line = _SHELL_LINE.match(line)
        magic_line = _MAGIC_LINE.match(line)
        if shell_line:
            indent, command = shell_line.groups()
            lines.append(f"{indent}_p2n_shell({command!r})")
        elif magic_line:
            lines.append(f"{magic_line.group(1)}pass")
        else:
            lines.append(line)
    return "\n".join(lines)


def run(cells, stream):
    """Run ``cells`` in one namespace; return the process exit code."""
    namespace = {
        "__name__": "__main__",
        "_p2n_shell": shell,
        "display": display,
    }
    for index, source in enumerate(cells):
        send(stream, event="start", cell=index)
        output = io.StringIO()
        try:
            code = compile(to_python(source), f"<cell {index}>", "exec")
            with (
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(output),
            ):
                # Running the notebook's code is the point of the check.
                # nosemgrep: exec-detected
                exec(code, namespace)  # noqa: S102
        except BaseException as exc:  # SystemExit too: report, don't exit
            send(
                stream,
                event="error",
                cell=index,
                error=f"{type(exc).__name__}: {exc}"[:MAX_TEXT],
                traceback="".join(
                    traceback.format_exception(
                        type(exc), exc, exc.__traceback__.tb_next
                    )
                )[-MAX_TEXT:],
                output=output.getvalue()[-MAX_TEXT:],
            )
            return 1
        send(
            stream,
            event="ok",
            cell=index,
            output=output.getvalue()[-MAX_OUTPUT:],
        )
    send(stream, event="finished")
    return 0


def main():
    cells = json.load(sys.stdin)["cells"]
    return run(cells, sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
