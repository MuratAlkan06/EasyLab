from alembic import op

revision = 'e5f6a7b8'
down_revision = 'd4e5f6a7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        create table template_fields (
          id                    uuid        primary key default gen_random_uuid(),
          project_id            uuid        not null references projects(id) on delete cascade,
          field_name            text        not null,
          display_order         int         not null default 0,
          reference_box         jsonb       not null,
          semantic_description  text,
          expected_format       jsonb,
          created_at            timestamptz not null default now(),
          updated_at            timestamptz not null default now(),

          constraint uq_template_fields_name_per_project unique (project_id, field_name),
          constraint template_fields_field_name_nonempty check (char_length(field_name) > 0)
        )
    """)
    op.execute("create index ix_template_fields_project_id on template_fields (project_id)")


def downgrade() -> None:
    op.execute("drop index ix_template_fields_project_id")
    op.execute("drop table template_fields")
