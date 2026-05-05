from alembic import op

revision = 'd4e5f6a7'
down_revision = 'c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        alter table projects
          add constraint fk_projects_reference_image
          foreign key (reference_image_id) references images(id) on delete set null
    """)


def downgrade() -> None:
    op.execute("alter table projects drop constraint fk_projects_reference_image")
