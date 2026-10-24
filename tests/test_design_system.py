"""Test that all templates and CSS adhere to the PropTally design system.

Ensures that every class used in a template is defined in CSS, no raw color values
or ad-hoc inline styles exist in markup, and the token hierarchy is respected.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_design_system import check_design_system


def test_design_system_conformance():
    """Fail if any template introduces undefined CSS classes or raw inline styles."""
    failures = check_design_system()
    assert not failures, "Design system violations found:\n" + "\n".join(f"  {f}" for f in failures)
