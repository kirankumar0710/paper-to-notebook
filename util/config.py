"""Read provider defaults from TOML files and API keys from the environment.

``config.toml`` in the project root ships the vendor defaults. Setting
``PAPER_TO_NOTEBOOK_CONFIG`` to another ``.toml`` file layers it on top: its
values override the defaults key by key (nested tables are merged too).

API keys are never read from a file, only from the environment variables
named by each section's ``api_key_env``.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from util.log import get_logger

__all__ = [
    "CONFIG_ENV_VAR",
    "Config",
    "ConfigError",
    "DEFAULT_CONFIG_PATH",
    "OUTPUT_DIR_ENV_VAR",
    "PROJECT_ROOT",
    "default_output_dir",
    "locate_output_dir",
    "resolve_output_dir",
    "find_api_key",
    "locate_api_key",
    "load_config",
    "parse_env_var_list",
]

CONFIG_ENV_VAR = "PAPER_TO_NOTEBOOK_CONFIG"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"

OUTPUT_DIR_ENV_VAR = "NOTEBOOK_OUTPUT_DIR"
_ENV_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# The merged config: {table name: {key: value}}.
Config = dict[str, dict[str, Any]]

logger = get_logger(__name__)


class ConfigError(ValueError):
    """A required setting is missing or invalid.

    The message always names the environment variable (or config key) to
    set, so it can be shown to the user as-is.
    """


def load_config(path: str | Path | None = None) -> Config:
    """Load the vendor defaults, then any override file on top.

    Args:
        path: Override file. Defaults to ``$PAPER_TO_NOTEBOOK_CONFIG``.

    Returns:
        The merged config.

    Raises:
        FileNotFoundError: If an override file is given but does not exist.
        ConfigError: If a file is not valid TOML, or is an old ``.ini``.
    """
    files = [DEFAULT_CONFIG_PATH]
    override = path or os.environ.get(CONFIG_ENV_VAR)
    if override:
        override_path = Path(override)
        if not override_path.is_file():
            raise FileNotFoundError(f"Config file not found: {override_path}")
        files.append(override_path)

    config: Config = {}
    for file in files:
        _merge(config, _read_toml(file))
    logger.info(
        "Loaded config: %s (tables: %s)",
        ", ".join(str(file) for file in files),
        ", ".join(config) or "<none>",
    )
    return config


def _read_toml(path: Path) -> Config:
    """Parse one config file; every top-level value must be a table."""
    if path.suffix.lower() == ".ini":
        raise ConfigError(
            f"{path} is an INI file; the config is now TOML. Rename it to "
            '.toml and quote string values, e.g. model = "gpt-5.5".'
        )
    if not path.is_file():
        logger.warning("Config file not found: %s", path)
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    for name, value in data.items():
        if not isinstance(value, dict):
            raise ConfigError(
                f"{path}: {name!r} must be a [table], not a top-level value."
            )
    return data


def _merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    """Overlay ``override`` on ``base`` in place, merging nested tables."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def parse_env_var_list(value: str | list[str] | None) -> tuple[str, ...]:
    """Return the variable names in ``api_key_env``.

    Accepts a list (``["A", "B"]``) or a comma-separated string (``"A, B"``).
    """
    if not value:
        return ()
    names = value.split(",") if isinstance(value, str) else value
    return tuple(str(name).strip() for name in names if str(name).strip())


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


def default_output_dir() -> Path:
    """The folder used when the output variable is not set: ``~/Downloads``."""
    return Path.home() / "Downloads"


def locate_output_dir(
    config: Config | None = None,
) -> tuple[Path, str]:
    """Return the output folder and where it came from.

    ``[app] output_dir`` in the config names the environment variable that
    holds the folder (``NOTEBOOK_OUTPUT_DIR`` if the key is missing). If
    that variable is not set, the folder is ``~/Downloads``. ``~`` and
    ``$VARIABLES`` in the value are expanded, and a relative path is
    resolved from the project root.

    Args:
        config: Parsed config. Defaults to :func:`load_config`.

    Returns:
        ``(folder, source)``, where source is ``"$VARIABLE"`` or
        ``"default"``.

    Raises:
        ConfigError: If ``[app] output_dir`` is not a variable name.
    """
    if config is None:
        config = load_config()
    env_var = (
        str(config.get("app", {}).get("output_dir") or "").strip()
        or OUTPUT_DIR_ENV_VAR
    )
    if not _ENV_VAR_NAME.match(env_var):
        raise ConfigError(
            f"[app] output_dir must name an environment variable "
            f"(e.g. {OUTPUT_DIR_ENV_VAR}), not {env_var!r}."
        )

    value = os.environ.get(env_var, "").strip()
    if not value:
        path = default_output_dir()
        logger.info(
            "Output folder: %s (default; set $%s to change)", path, env_var
        )
        return path, "default"

    path = Path(os.path.expandvars(value)).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    logger.info("Output folder: %s (from $%s)", path, env_var)
    return path, f"${env_var}"


def resolve_output_dir(
    config: Config | None = None,
) -> Path:
    """Return the folder where generated notebooks are saved.

    See :func:`locate_output_dir`; defaults to ``~/Downloads``.

    Raises:
        ConfigError: If ``[app] output_dir`` is not a variable name.
    """
    return locate_output_dir(config)[0]
