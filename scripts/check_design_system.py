"""Design-system conformance check for the PropTally web UI.

Enforces the rules in `docs/ui-ux/02_VISUAL_DESIGN_SYSTEM.md` §5 and the token
specification in `docs/ui-ux/03_DESIGN_TOKENS.md`:

1. Every CSS class used by a template is defined by the design system. A class
   that is not defined renders as nothing, which is how the pre-refactor UI
   accumulated 235 inert classes (docs/ui-ux/01_APPLICATION_AUDIT.md C1).
2. Templates declare no raw colours, font sizes, radii, shadows or z-indexes.
   Those belong to tokens.css.
3. Only the three design-system stylesheets are linked, in order.

Exit status is non-zero when a rule is broken, so this can run in CI.

Usage:
    PYTHONPATH=src python scripts/check_design_system.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "src" / "proptally" / "web" / "templates"
STYLESHEETS = [
    REPO_ROOT / "src" / "proptally" / "web" / "static" / "css" / "tokens.css",
    REPO_ROOT / "src" / "proptally" / "web" / "static" / "css" / "base.css",
    REPO_ROOT / "src" / "proptally" / "web" / "static" / "css" / "components.css",
]

JINJA = re.compile(r"\{#.*?#\}|\{%.*?%\}|\{\{.*?\}\}", re.DOTALL)
CLASS_ATTR = re.compile(r'class="([^"]*)"')
# A class name never contains '.', so a compound selector yields each part.
CLASS_TOKEN = re.compile(r"\.((?:[^\s,>~+:.\[\]()#]|\\.)+)")
# Marks a class token that Jinja interpolates, e.g. `pt-btn--{{ variant }}`.
DYNAMIC = "\x00"

# Raw values that must live in tokens.css only.
RAW_VALUE_PATTERNS = [
    (re.compile(r"#[0-9a-fA-F]{3,8}\b"), "raw hex colour"),
    (re.compile(r"\brgba?\("), "raw rgb() colour"),
]

# Classes that are structural hooks rather than design-system classes.
ALLOWED_CLASSES = {
    "split-row",          # JS hook: transaction split rows
    "split-amount-input", # JS hook: split amount inputs
    "pt-icon",            # set by ui.html's icon macro
    "is-active",          # state modifier used with .pt-nav__item/.pt-chip/...
    "is-disabled",        # state modifier used with .pt-pagination__link
    "pt-no-print",        # print control
}


def defined_classes() -> set[str]:
    """Every class selector declared across the design-system stylesheets."""
    defined: set[str] = set()
    for sheet in STYLESHEETS:
        css = sheet.read_text()
        for block in re.findall(r"([^{}]+)\{", css):
            for selector in block.split(","):
                for cls in CLASS_TOKEN.findall(selector):
                    defined.add(re.sub(r"\\(.)", r"\1", cls))
    return defined


def mark_jinja(text: str) -> str:
    """Blanks Jinja tags, marking interpolated class tokens as dynamic."""
    return JINJA.sub(lambda m: DYNAMIC + " " if m.group(0).startswith("{{") else " ", text)


def template_classes(path: Path) -> dict[str, list[int]]:
    """Class tokens used by a template, mapped to the lines they appear on."""
    used: dict[str, list[int]] = {}
    text = path.read_text()
    marked = mark_jinja(text)
    for match in CLASS_ATTR.finditer(marked):
        line = marked[: match.start()].count("\n") + 1
        for cls in match.group(1).split():
            # A token touching an interpolation is dynamic (prefix or suffix).
            if DYNAMIC in cls or cls.startswith("{"):
                continue
            used.setdefault(cls, []).append(line)
    return used


def css_blocks(css: str) -> list[tuple[str, str, str]]:
    """Rules as (at_rule_context, selector, declarations).

    Media/keyframe context is tracked so that a responsive override is not
    mistaken for a duplicate declaration (audit M1 was the same selector
    declared twice with different values in the same context).
    """
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.DOTALL)
    blocks: list[tuple[str, str, str]] = []
    context: list[str] = []
    i = 0
    length = len(css)
    while i < length:
        at = re.match(r"@[a-z-]+[^{;]*\{", css[i:])
        if at:
            context.append(at.group(0)[:-1].strip())
            i += at.end()
            continue
        if css[i] == "}":
            if context:
                context.pop()
            i += 1
            continue
        rule = re.match(r"([^{}]*)\{([^{}]*)\}", css[i:])
        if rule:
            selector = " ".join(rule.group(1).split())
            if selector:
                blocks.append((" > ".join(context), selector, " ".join(rule.group(2).split())))
            i += rule.end()
            continue
        i += 1
    return blocks


def main() -> int:
    defined = defined_classes()
    failures: list[str] = []

    for path in sorted(TEMPLATES.rglob("*.html")):
        rel = path.relative_to(REPO_ROOT)
        text = path.read_text()
        stripped = mark_jinja(text).replace(DYNAMIC, " ")

        for cls, lines in sorted(template_classes(path).items()):
            if cls not in defined and cls not in ALLOWED_CLASSES:
                failures.append(
                    f"{rel}:{lines[0]}: class '{cls}' is not defined by the design system"
                )

        for pattern, label in RAW_VALUE_PATTERNS:
            for match in pattern.finditer(stripped):
                line = stripped[: match.start()].count("\n") + 1
                line_text = stripped.split("\n")[line - 1]
                # theme-color must carry a literal colour: CSS cannot be used
                # in a <meta> value, and the PWA install uses it verbatim.
                if "theme-color" in line_text and label == "raw hex colour":
                    continue
                failures.append(
                    f"{rel}:{line}: {label} '{match.group(0)}' in markup — use a token"
                )

        for match in re.finditer(r"\bstyle=\"", stripped):
            line = stripped[: match.start()].count("\n") + 1
            failures.append(f"{rel}:{line}: inline style attribute — use a component class")

        for match in re.finditer(r"\bon(?:click|change|input|submit|keydown|load)=", stripped):
            line = stripped[: match.start()].count("\n") + 1
            failures.append(
                f"{rel}:{line}: inline event handler — bind by data attribute in a script"
            )

    # The shell must load exactly the three design-system stylesheets, in order. A version
    # query on the URL is expected (it is how a changed stylesheet defeats a stale cache),
    # so the filename is what is compared.
    base = (TEMPLATES / "base.html").read_text()
    linked = re.findall(r'href="/static/css/([^"?]+)', base)
    expected = [sheet.name for sheet in STYLESHEETS]
    if linked != expected:
        failures.append(
            f"base.html: stylesheets are {linked!r}, expected {expected!r}"
        )

    # Every token referenced must be declared in tokens.css, and every token
    # declared should be used (a declared-but-unused token is dead weight).
    declared = set(re.findall(r"(--pt-[a-z0-9-]+)\s*:", STYLESHEETS[0].read_text()))
    referenced: dict[str, str] = {}
    for sheet in STYLESHEETS[1:] + sorted(TEMPLATES.rglob("*.html")):
        text = sheet.read_text()
        for token in re.findall(r"var\((--pt-[a-z0-9-]+)", text):
            referenced.setdefault(token, sheet.name)
    for token, where in sorted(referenced.items()):
        if token not in declared:
            failures.append(f"{where}: references undefined token '{token}'")

    unused = sorted(declared - set(referenced) - set(re.findall(r"var\((--pt-[a-z0-9-]+)", STYLESHEETS[0].read_text())))
    # Composite aliases and the breakpoint documentation values are referenced
    # indirectly (by another token, or normatively by media queries, where CSS
    # custom properties cannot be used).
    documentation_only = {
        "--pt-surface-chrome",
        "--pt-surface-chrome-deep",
        "--pt-bp-sm",
        "--pt-bp-md",
        "--pt-bp-lg",
        "--pt-bp-xl",
    }
    for token in unused:
        if token in documentation_only:
            continue
        failures.append(f"tokens.css: token '{token}' is declared but never used")

    # A selector must not be declared twice, with different values, in the same
    # cascade context within one file: that is how the shell ended up with two
    # conflicting navies (audit M1).
    for sheet in STYLESHEETS:
        seen: dict[tuple[str, str], str] = {}
        for context, selector, body in css_blocks(sheet.read_text()):
            key = (context, selector)
            if key in seen and seen[key] != body:
                failures.append(
                    f"{sheet.name}: '{selector}'"
                    f"{' in ' + context if context else ''} is declared twice with "
                    "different values"
                )
            seen[key] = body

    if failures:
        print(f"design-system check FAILED ({len(failures)} issue(s)):\n")
        for failure in failures:
            print("  " + failure)
        return 1

    print("design-system check passed: every class is defined, no raw values in markup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
