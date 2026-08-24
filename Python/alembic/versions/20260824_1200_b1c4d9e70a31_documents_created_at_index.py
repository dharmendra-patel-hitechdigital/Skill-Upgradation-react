"""index documents.created_at for the cross-owner admin listing

The admin document list selects across every owner ordered by ``created_at``
DESC. ``ix_documents_owner_status_created`` cannot serve that query: its leading
column is ``owner_id``, which the admin query does not constrain. The result is
a full table scan plus a filesort on every page of the admin panel.

No data is touched and no column changes - this is an index-only migration, so
it is safe to run online and trivially reversible.

Revision ID: b1c4d9e70a31
Revises: fa37aa971645
Create Date: 2026-08-24 12:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = 'b1c4d9e70a31'
down_revision: str | None = 'fa37aa971645'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = 'ix_documents_created_at'


def upgrade() -> None:
    # batch_alter_table for SQLite compatibility, matching the initial revision.
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.create_index(INDEX_NAME, ['created_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_index(INDEX_NAME)
