"""Read provider defaults from INI files and API keys from the environment.

``config.ini`` in the project root ships the vendor defaults. Setting
``PAPER_TO_NOTEBOOK_CONFIG`` to another ``.ini`` file layers it on top: its
values override the defaults key by key.

API keys are never read from a file, only from the environment variables
named by each section's ``api_key_env``.
"""

from __future__ import annotations

import configparser
import os
import re
from pathlib import Path

from util.log import get_logger

__all__ = [
    "CONFIG_ENV_VAR",
    "ConfigError",
    "DEFAULT_CONFIG_PATH",
    "OUTPUT_DIR_ENV_VAR",
    "PROJECT_ROOT",
    "resolve_output_dir",
    "find_api_key",
    "locate_api_key",
    "load_config",
    "parse_env_var_list",
]

CONFIG_ENV_VAR = "PAPER_TO_NOTEBOOK_CONFIG"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.ini"

OUTPUT_DIR_ENV_VAR = "NOTEBOOK_OUTPUT_DIR"
_ENV_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

logger = get_logger(__name__)


class ConfigError(ValueError):
    """A required setting is missing or invalid.

    The message always names the environment variable (or config key) to
    set, so it can be shown to the user as-is.
    """


def load_config(
    path: str | Path | None = None,
) -> configparser.ConfigParser:
    """Load the vendor defaults, then any override file on top.

    Args:
        path: Override file. Defaults to ``$PAPER_TO_NOTEBOOK_CONFIG``.

    Returns:
        The merged config.

    Raises:
        FileNotFoundError: If an override file is given but does not exist.
    """
    files = [DEFAULT_CONFIG_PATH]
    override = path or os.environ.get(CONFIG_ENV_VAR)
    if override:
        override_path = Path(override)
        if not override_path.is_file():
            raise FileNotFoundError(f"Config file not found: {override_path}")
        files.append(override_path)

    config = configparser.ConfigParser()
    loaded = config.read(files, encoding="utf-8")
    logger.info(
        "Loaded config: %s (sections: %s)",
        ", ".join(loaded) or "<none>",
        ", ".join(config.sections()) or "<none>",
    )
    return config


def parse_env_var_list(value: str) -> tuple[str, ...]:
    """Split a comma-separated ``api_key_env`` value into variable names."""
    return tuple(name.strip() for name in value.split(",") if name.strip())


def locate_api_key(env_vars: tuple[str, ...]) -> tuple[str, str] | None:
    """Return ``(variable name, key)`` for the first set variable, if any."""
    for name in env_vars:
        value = os.environ.get(name, "").strip()
        if value:
            return name, value
    return None


def find_api_key(env_vars: tuple[str, ...]) -> str | None:
    """Return the first non-empty value among ``env_vars``, if any."""
    found = locate_api_key(env_vars)
    return found[1] if found else None


def resolve_output_dir(
    config: configparser.ConfigParser | None = None,
) -> Path:
    """Return the folder where generated notebooks are saved.

    ``[app] output_dir`` in the config names the environment variable that
    holds the folder (``NOTEBOOK_OUTPUT_DIR``). There is no default: the
    variable must be set. ``~`` and ``$VARIABLES`` in its value are
    expanded, and a relative path is resolved from the project root.

    Args:
        config: Parsed config. Defaults to :func:`load_config`.

    Raises:
        ConfigError: If ``[app] output_dir`` is missing or is not a variable
            name, or the variable it names is not set.
    """
    if config is None:
        config = load_config()
    env_var = config.get("app", "output_dir", fallback="").strip()
    if not env_var:
        raise ConfigError(
            "No output folder configured: set 'output_dir' under [app] in "
            f"config.ini to an environment variable name, e.g. "
            f"{OUTPUT_DIR_ENV_VAR}."
        )
    if not _ENV_VAR_NAME.match(env_var):
        raise ConfigError(
            f"[app] output_dir must name an environment variable "
            f"(e.g. {OUTPUT_DIR_ENV_VAR}), not {env_var!r}."
        )

    value = os.environ.get(env_var, "").strip()
    if not value:
        raise ConfigError(
            f"No output folder: export {env_var}, e.g. "
            f"export {env_var}=~/Downloads"
        )

    path = Path(os.path.expandvars(value)).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    logger.info("Output folder: %s (from $%s)", path, env_var)
    return path
