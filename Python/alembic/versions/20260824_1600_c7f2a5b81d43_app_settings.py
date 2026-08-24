"""add app_settings for runtime configuration

Backs the admin panel's AI-engine picker: which analysis provider runs is an
operational choice that must change without a redeploy, so it cannot live in the
environment alone.

Key/value rather than a column per setting, so adding the next runtime knob is a
row and not a migration. Secrets do not go in this table - the column is
unencrypted and the value is echoed back over the admin API.

Revision ID: c7f2a5b81d43
Revises: b1c4d9e70a31
Create Date: 2026-08-24 16:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'c7f2a5b81d43'
down_revision: str | None = 'b1c4d9e70a31'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'app_settings',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', sa.String(length=255), nullable=True),
        sa.Column('updated_by_id', sa.Integer(), nullable=True),
        sa.Column(
            'updated_at',
            sa.DateTime(),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        # SET NULL, not CASCADE: deleting the administrator who last changed a
        # setting must not delete the setting.
        sa.ForeignKeyConstraint(['updated_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade() -> None:
    op.drop_table('app_settings')
