"""Render a candidate WinSW sidecar without replacing external credential references.

Release payloads intentionally contain only symbolic credential-path placeholders.
The installed WinSW sidecar is the host-owned boundary that binds those placeholders
to existing external credential files.  A deployment must preserve that binding
without reading either credential file or serializing the path values into evidence.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from xml.etree import ElementTree
from xml.sax.saxutils import escape


class SidecarRenderError(RuntimeError):
    """Raised when an installed WinSW sidecar cannot be safely rendered."""


_CREDENTIAL_PATH_ENVIRONMENTS = (
    "LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH",
    "LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH",
)
_ENVIRONMENT_PLACEHOLDER = re.compile(r"%[^%]+%")


@dataclass(frozen=True)
class RenderedWinSWSidecar:
    """Candidate XML bytes with preserved host-owned credential path references."""

    xml: bytes
    sha256: str
    preserved_environment_names: tuple[str, ...]


def _environment_values(root: ElementTree.Element, *, role: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for node in root.findall("env"):
        name = node.get("name")
        value = node.get("value")
        if name is None or value is None:
            raise SidecarRenderError(f"{role} sidecar contains an invalid environment entry")
        if name in values:
            raise SidecarRenderError(f"{role} sidecar duplicates environment {name}")
        values[name] = value
    return values


def _parse_sidecar(path: Path, *, role: str) -> ElementTree.Element:
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise SidecarRenderError(f"{role} sidecar XML is unavailable or invalid") from error
    if root.tag != "service":
        raise SidecarRenderError(f"{role} sidecar root is invalid")
    return root


def _require_candidate_identity(
    root: ElementTree.Element, *, expected_service_id: str, expected_component: str
) -> None:
    if root.findtext("id", default="") != expected_service_id:
        raise SidecarRenderError("candidate sidecar service identity is invalid")
    arguments = root.findtext("arguments", default="")
    if (
        re.search(rf"(?:^|\s)--component\s+{re.escape(expected_component)}(?:\s|$)", arguments)
        is None
    ):
        raise SidecarRenderError("candidate sidecar component identity is invalid")


def render_candidate_winsw_sidecar(
    *,
    candidate_template: Path,
    installed_sidecar: Path,
    expected_service_id: str,
    expected_component: str,
) -> RenderedWinSWSidecar:
    """Render a candidate sidecar while preserving only existing external key paths.

    The candidate must retain the two symbolic placeholders.  The installed sidecar
    must provide absolute path references for them; environment placeholders are
    rejected because a LocalSystem service does not inherit the deploy user's
    environment.  This function never reads credential file contents.
    """

    candidate_root = _parse_sidecar(candidate_template, role="candidate")
    installed_root = _parse_sidecar(installed_sidecar, role="installed")
    _require_candidate_identity(
        candidate_root,
        expected_service_id=expected_service_id,
        expected_component=expected_component,
    )
    candidate_environment = _environment_values(candidate_root, role="candidate")
    installed_environment = _environment_values(installed_root, role="installed")
    try:
        template_text = candidate_template.read_text(encoding="utf-8")
    except OSError as error:
        raise SidecarRenderError("candidate sidecar XML is unavailable or invalid") from error

    rendered = template_text
    for name in _CREDENTIAL_PATH_ENVIRONMENTS:
        placeholder = f"%{name}%"
        if candidate_environment.get(name) != placeholder or template_text.count(placeholder) != 1:
            raise SidecarRenderError("candidate sidecar credential placeholder is invalid")
        prior_value = installed_environment.get(name)
        if (
            prior_value is None
            or not PureWindowsPath(prior_value).is_absolute()
            or _ENVIRONMENT_PLACEHOLDER.search(prior_value) is not None
        ):
            raise SidecarRenderError(
                "installed sidecar credential reference must be an absolute path"
            )
        rendered = rendered.replace(placeholder, escape(prior_value, {'"': "&quot;"}))

    try:
        rendered_root = ElementTree.fromstring(rendered)
    except ElementTree.ParseError as error:
        raise SidecarRenderError("rendered sidecar XML is invalid") from error
    rendered_environment = _environment_values(rendered_root, role="rendered")
    if any(
        rendered_environment.get(name) != installed_environment[name]
        for name in _CREDENTIAL_PATH_ENVIRONMENTS
    ):
        raise SidecarRenderError("rendered sidecar credential reference changed")
    payload = rendered.encode("utf-8")
    return RenderedWinSWSidecar(
        xml=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        preserved_environment_names=_CREDENTIAL_PATH_ENVIRONMENTS,
    )
