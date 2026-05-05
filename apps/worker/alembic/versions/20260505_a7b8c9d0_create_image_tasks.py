from alembic import op

revision = 'a7b8c9d0'
down_revision = 'f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        create table image_tasks (
          id         uuid        primary key default gen_random_uuid(),
          job_id     uuid        not null references jobs(id) on delete cascade,
          image_id   uuid        not null references images(id) on delete cascade,
          status     text        not null default 'pending',
          error      text,
          updated_at timestamptz not null default now(),

          constraint uq_image_tasks_job_image unique (job_id, image_id),
          constraint image_tasks_status_check check (
            status in ('pending','detecting','extracting','done','failed','not_found')
          )
        )
    """)
    op.execute("create index ix_image_tasks_job_id on image_tasks (job_id)")


def downgrade() -> None:
    op.execute("drop index ix_image_tasks_job_id")
    op.execute("drop table image_tasks")
