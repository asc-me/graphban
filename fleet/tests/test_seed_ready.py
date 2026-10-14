"""until does not seed a blocked item, even when a supervisor asks (GRPH-1032)."""
from gbfleet.until import _seed_ready


def test_a_blocked_item_is_not_a_seed():
    """Sabotage: drop the status==blocked clause — a 'Do not launch' row is spawned."""
    assert not _seed_ready({"status": "blocked", "blocker": "Do not launch"})
    assert not _seed_ready({"status": "blocked", "blocker": ""})
    assert not _seed_ready({"status": "next", "blocker": "Do not launch"})
    assert not _seed_ready({"status": "review"})
    assert _seed_ready({"status": "next", "blocker": ""})
    assert _seed_ready({"status": "backlog"})
