"""ContractAgent: the browser and the backend must agree.

This is the agent that catches the classic "frontend calls a route that does
not exist" or "page template is missing" class of bug, which no amount of
unit testing on either side alone will find.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.api.web import PAGES, TEMPLATES, WEB_ROOT
from app.main import app

JS_DIR = WEB_ROOT / "static" / "js"
API_MARKER = "/api/v1/"
AREAS = {"public", "user", "admin"}

_TEMPLATE_EXPRESSION = "${PARAM}"
_PATH_LITERAL = re.compile(r"[`\"'](/api/v1/[^`\"']*)[`\"']")


def _strip_template_expressions(source: str) -> str:
    """Replace ``${...}`` (including nested braces and inner quotes) with a token.

    Doing this first lets a simple string-literal regex read JavaScript template
    literals that contain ``"`` inside their interpolation, e.g.
    ``/api/v1/catalog/${kind === "SERIES" ? "series" : "packages"}/...``.
    """

    out: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        if source.startswith("${", index):
            depth = 0
            cursor = index
            while cursor < length:
                if source.startswith("${", cursor):
                    depth += 1
                    cursor += 2
                    continue
                if source[cursor] == "}":
                    depth -= 1
                    cursor += 1
                    if depth == 0:
                        break
                    continue
                cursor += 1
            out.append(_TEMPLATE_EXPRESSION)
            index = cursor
        else:
            out.append(source[index])
            index += 1
    return "".join(out)


def _segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def _is_param(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}")


def _matches(js_segments: list[str], backend_segments: list[str]) -> bool:
    """A JS segment matches if it is a runtime expression or the backend is a path param."""

    if len(js_segments) != len(backend_segments):
        return False
    for js_segment, backend_segment in zip(js_segments, backend_segments):
        if js_segment == "{param}" or _is_param(backend_segment):
            continue
        if js_segment != backend_segment:
            return False
    return True


def _frontend_paths() -> set[str]:
    paths: set[str] = set()
    for module in sorted(JS_DIR.glob("*.js")):
        source = _strip_template_expressions(module.read_text(encoding="utf-8"))
        for match in _PATH_LITERAL.findall(source):
            path = match.split("?", 1)[0]
            # The earlier pass replaced runtime expressions with "${PARAM}";
            # collapse both that token and any literal path param to "{param}".
            path = path.replace(_TEMPLATE_EXPRESSION, "{param}")
            path = re.sub(r"\{[^}]*\}", "{param}", path)
            paths.add(path.rstrip("/") or "/")
    return paths


def _backend_paths() -> list[list[str]]:
    return [_segments(path) for path in app.openapi()["paths"]]


def test_frontend_js_only_calls_routes_that_exist() -> None:
    frontend = _frontend_paths()
    assert frontend, "no /api/v1 calls were found in the frontend modules"

    backend = _backend_paths()
    missing = sorted(
        path for path in frontend if not any(_matches(_segments(path), candidate) for candidate in backend)
    )
    assert not missing, f"the frontend calls endpoints that do not exist: {missing}"


def test_every_page_has_a_template_file() -> None:
    missing = [page for page, template in PAGES.items() if not (TEMPLATES / template).is_file()]
    assert not missing, f"pages without a template file: {missing}"


def test_every_page_template_declares_a_known_area() -> None:
    problems: list[Path] = []
    for page, template in PAGES.items():
        path = TEMPLATES / template
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        area = re.search(r'data-area="([^"]+)"', text)
        page_name = re.search(r'data-page="([^"]+)"', text)
        if area is None or area.group(1) not in AREAS or page_name is None:
            problems.append(path)
    assert not problems, f"page templates missing a valid data-area/data-page: {problems}"
