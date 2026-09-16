"""What the two HTML views share: a document wrapper and one escaping rule.

Both pages carry their data as a JSON literal inside a `<script>` element
rather than fetching it, because both have to open with no network at all. That
choice creates exactly one hazard, and it is the same hazard for both pages, so
it is solved once here.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any, Final

#: Where the payload goes in a template.
SLOT: Final = "/*__DATA__*/null"


def script_literal(payload: dict[str, Any]) -> str:
    """JSON safe to sit inside a `<script>` element.

    Every string in these payloads was written by a model -- the task, the SQL,
    the tool results, the report. A `</script>` anywhere in any of them closes
    the element early and the rest of the page becomes markup the browser will
    happily run. Escaping on the way into the DOM does not help: by then the
    damage is already in the document. So `<` never reaches the page as itself,
    and the two line separators that are legal in JSON but not in a JavaScript
    string literal go with it.
    """
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (text.replace("<", "\\u003c")
                .replace("\u2028", "\\u2028")
                .replace("\u2029", "\\u2029"))


def document(template: str, payload: dict[str, Any]) -> str:
    """One self-contained page: template plus its data, nothing fetched."""
    return (
        '<!doctype html>\n<html lang="zh-Hant">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"</head>\n<body>\n{template.replace(SLOT, script_literal(payload))}\n"
        "</body>\n</html>\n"
    )


def template(name: str) -> str:
    return resources.files(__package__).joinpath(name).read_text("utf-8")


__all__ = ["SLOT", "document", "script_literal", "template"]
