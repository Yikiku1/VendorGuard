"""prevent audit event mutations

Revision ID: 9be8c214387a
Revises: e070d366b661
Create Date: 2026-08-31 21:41:23.492564

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9be8c214387a"
down_revision: str | Sequence[str] | None = "e070d366b661"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """阻止已有审计事件被更新或删除。"""

    op.execute(
        sa.text(
            """
            CREATE FUNCTION prevent_audit_event_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'audit events are append-only'
                    USING ERRCODE = '55000';
            END;
            $$;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER audit_events_prevent_mutation
            BEFORE UPDATE OR DELETE ON audit_events
            FOR EACH ROW
            EXECUTE FUNCTION prevent_audit_event_mutation();
            """
        )
    )


def downgrade() -> None:
    """移除审计事件更新和删除保护。"""

    op.execute(
        sa.text(
            """
            DROP TRIGGER IF EXISTS audit_events_prevent_mutation
            ON audit_events;
            """
        )
    )
    op.execute(
        sa.text(
            """
            DROP FUNCTION IF EXISTS prevent_audit_event_mutation();
            """
        )
    )
