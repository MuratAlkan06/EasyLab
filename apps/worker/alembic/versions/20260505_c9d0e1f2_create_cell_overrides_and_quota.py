from alembic import op

revision = 'c9d0e1f2'
down_revision = 'b8c9d0e1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        create table cell_overrides (
          id              uuid        primary key default gen_random_uuid(),
          project_id      uuid        not null references projects(id) on delete cascade,
          image_id        uuid        not null references images(id) on delete cascade,
          field_id        uuid        not null references template_fields(id) on delete cascade,
          corrected_value jsonb       not null,
          corrected_at    timestamptz not null default now(),

          constraint uq_cell_overrides_project_image_field unique (project_id, image_id, field_id)
        )
    """)
    op.execute("create index ix_cell_overrides_project_id on cell_overrides (project_id)")
    op.execute("""
        create table workspace_quota (
          workspace_id  uuid   primary key references workspaces(id) on delete cascade,
          jobs_today    int    not null default 0,
          images_today  int    not null default 0,
          tokens_today  bigint not null default 0,
          quota_date    date   not null default current_date
        )
    """)


def downgrade() -> None:
    op.execute("drop table workspace_quota")
    op.execute("drop index ix_cell_overrides_project_id")
    op.execute("drop table cell_overrides")
