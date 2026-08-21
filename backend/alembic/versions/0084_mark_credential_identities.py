"""Mark rows stamped by a credential rather than by an agent (GRPH-437).

`items.reviewed_by` and its neighbours hold whatever `mcp_server` resolved the caller to be:
a registered agent's id, or — in the single-agent posture — the API key's NAME, with nothing
distinguishing the two. On 2026-08-21 four items came out reviewed by `wave-refetch-2`, the
label on a key minted for an unrelated probe, and no reader could have told that from the row.

`caller_identity` now marks the fallback `key:<name>`. This backfill exists because that mark
must be applied to what is already stored, not only to what arrives next: the self-review ban
compares `item.built_by == agent_id`, so an item claimed under the old bare form and signed
off under the new prefixed one would compare unequal and the ban would pass silently. Leaving
history alone would introduce exactly the failure the change is meant to prevent.

WHICH ROWS. A value is a credential if it names an api_key AND is not an agent id. Both halves
are needed: agent ids are the common case and must not be touched, and a string matching
neither is left alone because we cannot say what it is. Agent ids win a tie — an agent by that
id demonstrably exists, and mislabelling a real agent is the worse error.

WHAT THIS CANNOT DO, stated because a silent gap is what is being fixed: a key that has since
been RENAMED or deleted leaves rows this cannot recognise, and they keep their bare form. They
are indistinguishable from an agent id by construction — that is the defect, and the fix is
forward-looking. `assignee` is included but is the loosest of the five: it may hold a name a
human chose, so a person whose name exactly matches an API key's would be relabelled. No such
row exists in any current deployment; it is written down rather than assumed away.

Revision ID: 0084
Revises: 0083
"""
from alembic import op

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None

COLUMNS = ("claimed_by", "built_by", "assignee", "reviewed_by", "review_claimed_by")


def upgrade() -> None:
    for col in COLUMNS:
        op.execute(f"""
            UPDATE items SET {col} = 'key:' || {col}
            WHERE {col} IS NOT NULL
              AND {col} <> ''
              AND {col} NOT LIKE 'key:%'
              AND {col} IN (SELECT name FROM api_keys WHERE name IS NOT NULL)
              AND {col} NOT IN (SELECT id FROM agents)
        """)


def downgrade() -> None:
    # Reversible without the api_keys lookup: the prefix is the whole of what was added, and
    # nothing else in these columns is allowed to start with it.
    for col in COLUMNS:
        op.execute(f"""
            UPDATE items SET {col} = substr({col}, 5)
            WHERE {col} LIKE 'key:%'
        """)
