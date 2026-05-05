from alembic import op

revision = 'b2c3d4e5'
down_revision = 'a1b2c3d4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        create table projects (
          id                 uuid        primary key default gen_random_uuid(),
          workspace_id       uuid        not null references workspaces(id) on delete cascade,
          name               text        not null,
          reference_image_id uuid,
          status             text        not null default 'draft',
                              -- draft | annotated | processing | done
          template_quality   text,       -- null | 'degraded'; set by FastAPI during template generation
          created_at         timestamptz not null default now(),
          updated_at         timestamptz not null default now(),

          constraint projects_name_workspace_unique unique (workspace_id, name),
          constraint projects_status_check check (
            status in ('draft','annotated','processing','done')
          )
        )
    """)


def downgrade() -> None:
    op.execute("drop table projects")
