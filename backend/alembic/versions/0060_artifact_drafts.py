"""The drafted artifact and how it may be installed (GRPH-308 / PRD-16).

*"Render the real artifact — the thing someone could install today, not a summary or a
TODO."* A recommendation that says "you should write a skill for this" moves no work; the
skill does.

`install_class` is the human boundary, and it is a property of the TARGET rather than of
the artifact's quality:

    file_additive   a wholly new self-contained file — may install on approval
    shared_surgery  an edit inside a file many other things live in — never written

The second is never installed however confident anything is. PRD-16's non-goal is explicit
that generated artifacts are proposed and the human boundary does not move in this PRD, and
a machine editing AGENTS.md or a settings file is precisely the move that loses trust once
and keeps it lost.

`draft_hash` is sha256 of (statement + evidence). An unchanged lesson set costs zero model
calls across runs, which is what makes a scheduled drafting pass affordable to leave on —
and a pass nobody can afford to leave on is one that gets disabled.

Revision ID: 0060
Revises: 0059
"""
from alembic import op
import sqlalchemy as sa

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, col in (
        ("draft", sa.Text()),
        ("draft_path", sa.String()),
        ("install_class", sa.String()),
        ("draft_hash", sa.String()),
    ):
        op.add_column("artifact_recommendations",
                      sa.Column(name, col, nullable=False, server_default=""))


def downgrade() -> None:
    for name in ("draft_hash", "install_class", "draft_path", "draft"):
        op.drop_column("artifact_recommendations", name)
