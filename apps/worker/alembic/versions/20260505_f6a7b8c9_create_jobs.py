from alembic import op

revision = 'f6a7b8c9'
down_revision = 'e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        create table jobs (
          id              uuid        primary key default gen_random_uuid(),
          project_id      uuid        not null references projects(id) on delete cascade,
          kind            text        not null default 'process_batch',
          status          text        not null default 'queued',
          payload         jsonb       not null default '{}',
          attempts        int         not null default 0,
          max_attempts    int         not null default 3,
          error           text,
          progress_total  int         not null default 0,
          progress_done   int         not null default 0,
          created_at      timestamptz not null default now(),
          started_at      timestamptz,
          finished_at     timestamptz,
          heartbeat_at    timestamptz,
          paused_until    timestamptz,
          locked_by       text,

          constraint jobs_status_check check (
            status in ('queued','running','succeeded','failed','cancelled')
          )
        )
    """)
    op.execute("""
        create unique index uq_jobs_one_active_per_project
          on jobs (project_id) where status in ('queued','running')
    """)
    op.execute("""
        create index ix_jobs_status_created on jobs (status, created_at)
          where status = 'queued'
    """)


def downgrade() -> None:
    op.execute("drop index ix_jobs_status_created")
    op.execute("drop index uq_jobs_one_active_per_project")
    op.execute("drop table jobs")
