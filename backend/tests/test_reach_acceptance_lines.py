"""Acceptance and sabotage lines describe tests, not deployed work (GRPH-1394)."""
from app.services import reach as reach_svc

SABOTAGE = (
    "Sabotage: mark the key row inactive in the fixture and rotate the production key; "
    "the test must fail."
)
FRAMING = "Change the production credential and rotate the production key today."


def test_sabotage_line_inside_acceptance_does_not_fire():
    text = f"Ordinary framing.\n\n## Acceptance\n- Works.\n- {SABOTAGE}\n"
    assert reach_svc.signals(text) == []


def test_bare_sabotage_line_does_not_fire():
    assert reach_svc.signals(f"Intro.\n{SABOTAGE}\n") == []


def test_framing_credential_wording_still_fires():
    assert reach_svc.signals(f"{FRAMING}\n\n## Acceptance\n- Works.\n")


def test_framing_after_acceptance_section_still_fires():
    text = f"## Acceptance\n- Works.\n\n## Notes\n{FRAMING}\n"
    assert reach_svc.signals(text)


def test_credential_wording_in_acceptance_does_not_fire():
    assert reach_svc.signals(f"## Acceptance\n- {FRAMING}\n") == []
