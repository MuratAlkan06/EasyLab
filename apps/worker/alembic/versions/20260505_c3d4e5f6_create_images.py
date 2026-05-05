from alembic import op

revision = 'c3d4e5f6'
down_revision = 'b2c3d4e5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        create table images (
          id           uuid        primary key default gen_random_uuid(),
          project_id   uuid        not null references projects(id) on delete cascade,
          storage_path   text        not null,
          thumbnail_path text,                 -- set by FastAPI after thumbnail is generated
          filename       text        not null,
          mime_type    text        not null,
          size_bytes   bigint,
          width_px     int,
          height_px    int,
          status       text        not null default 'pending_upload',
          is_reference boolean     not null default false,
          created_at   timestamptz not null default now(),
          updated_at   timestamptz not null default now(),

          constraint images_status_check check (
            status in ('pending_upload','uploaded','processing','done','failed')
          )
        )
    """)
    op.execute("""
        create unique index uq_images_one_reference_per_project
          on images (project_id) where is_reference = true
    """)
    op.execute("create index ix_images_project_id on images (project_id)")


def downgrade() -> None:
    op.execute("drop index ix_images_project_id")
    op.execute("drop index uq_images_one_reference_per_project")
    op.execute("drop table images")
