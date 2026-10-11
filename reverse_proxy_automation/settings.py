"""Destination-neutral primitives: secret references and switch coercion.

These used to live in service.py. They moved here so a destination plugin can
use them without importing the service that loads it, which would turn the
plugin into a dependency of its own host.
"""

import os
import re


# A secret field holds either the literal secret or a reference such as
# "env:NPM_LAB_SECRET". The reference is read from the environment the dashboard
# was started from when a client is built, so the settings file can stay
# secret-free while the value never has to pass through the browser.
SECRET_ENV_PREFIX = "env:"
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def secret_env_name(value):
    """Environment variable a secret field references, or None for a literal value."""
    text = str(value or "")
    if text[: len(SECRET_ENV_PREFIX)].lower() != SECRET_ENV_PREFIX:
        return None
    return text[len(SECRET_ENV_PREFIX) :].strip()


def resolve_secret(value, environ=None):
    """The literal secret, or the value of the variable an "env:" reference names.

    A reference to an unset variable resolves to "", so callers can report the
    missing variable instead of sending an empty secret to an API.
    """
    name = secret_env_name(value)
    if name is None:
        return str(value or "")
    return str((os.environ if environ is None else environ).get(name, ""))


def as_bool(value, default):
    """Coerce a stored or submitted switch to a bool, falling back to the default.

    A hand-edited settings file may hold "false" or 0 rather than a JSON bool, so
    the recognisable spellings are accepted; anything unrecognised keeps the
    default instead of failing the whole file.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def check_reference_name(description, value, allowed_pattern=None, hint=""):
    """Reject env references that do not follow the field's supported naming rule."""
    name = secret_env_name(value)
    if name is None:
        return
    if not ENV_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"{description} references '{name}', which is not a valid environment variable name"
        )
    if allowed_pattern is not None and not allowed_pattern.fullmatch(name):
        raise ValueError(
            f"{description} must reference the variable assigned to this secret field{hint}"
        )


def plaintext_secret_fields(fields):
    """Descriptions from (description, value) pairs that hold a literal secret.

    A field holding an "env:" reference is not listed, because the reference is
    not itself a secret. Only the description is returned, so the result is safe
    to print at startup and to send to the browser.
    """
    return [
        description
        for description, value in fields
        if str(value or "") and secret_env_name(value) is None
    ]


def check_secret_references(fields, environ=None):
    """Raise ValueError naming every "env:" reference that resolves to nothing.

    Without this check an unset variable would look like "no password
    configured" and a run would quietly talk to the API unauthenticated. The
    wording is shared so every destination reports the same way.
    """
    missing = unresolved_secret_references(fields, environ)
    if not missing:
        return
    details = "; ".join(
        f"{description} references {name}, which is not set" for description, name in missing
    )
    raise ValueError(f"{details}. Set it in the environment the dashboard was started from")


def unresolved_secret_references(fields, environ=None):
    """Every (description, variable name) whose "env:" reference is not set."""
    unresolved = []
    for description, value in fields:
        name = secret_env_name(value)
        if name and not resolve_secret(value, environ):
            unresolved.append((description, name))
    return unresolved
