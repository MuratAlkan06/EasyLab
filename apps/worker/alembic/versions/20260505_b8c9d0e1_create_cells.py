from alembic import op

revision = 'b8c9d0e1'
down_revision = 'a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        create table cells (
          id                  uuid         primary key default gen_random_uuid(),
          job_id              uuid         not null references jobs(id) on delete cascade,
          project_id          uuid         not null references projects(id) on delete cascade,
          image_id            uuid         not null references images(id) on delete cascade,
          field_id            uuid         not null references template_fields(id) on delete cascade,
          raw_text            text,
          parsed_value        jsonb,
          unit_seen           text,
          legible             boolean,
          box_confidence      numeric(4,3) not null default 0,
          value_confidence    numeric(4,3) not null default 0,
          combined_confidence numeric(4,3),
          status              text         not null default 'ok',
          failure_reason      text,
          validation_error    text,
          detected_box        jsonb,
          crop_path           text,
          model_versions      jsonb        not null default '{}',
          retried             boolean      not null default false,
          created_at          timestamptz  not null default now(),
          updated_at          timestamptz  not null default now(),

          constraint uq_cells_job_image_field unique (job_id, image_id, field_id),
          constraint cells_status_check check (
            status in ('ok','low_confidence','needs_review','failed')
          ),
          constraint cells_box_confidence_range check (box_confidence between 0 and 1),
          constraint cells_value_confidence_range check (value_confidence between 0 and 1)
        )
    """)
    op.execute("create index ix_cells_job_id on cells (job_id)")
    op.execute("create index ix_cells_project_id on cells (project_id)")
    op.execute("create index ix_cells_image_id on cells (image_id)")
    op.execute("""
        create index ix_cells_job_status on cells (job_id, status)
          where status in ('low_confidence','needs_review','failed')
    """)


def downgrade() -> None:
    op.execute("drop index ix_cells_job_status")
    op.execute("drop index ix_cells_image_id")
    op.execute("drop index ix_cells_project_id")
    op.execute("drop index ix_cells_job_id")
    op.execute("drop table cells")
