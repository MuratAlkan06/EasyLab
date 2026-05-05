from alembic import op

revision = 'a1b2c3d4'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        create table workspaces (
          id         uuid        primary key default gen_random_uuid(),
          created_at timestamptz not null    default now()
        )
    """)


def downgrade() -> None:
    op.execute("drop table workspaces")
