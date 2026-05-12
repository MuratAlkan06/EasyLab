"""Enable Supabase Realtime on jobs and image_tasks.

The browser subscribes to row changes on these two tables so the process page
can react instantly instead of polling. Realtime requires each watched table to
be a member of the ``supabase_realtime`` publication (Supabase creates this
publication on every project).

Idempotent: we only ADD the tables when they aren't already members, so
re-running `alembic upgrade head` after the publication was edited via the
Supabase dashboard doesn't error.
"""

from alembic import op


revision = "d0e1f2a3"
down_revision = "c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'supabase_realtime') THEN
            IF NOT EXISTS (
              SELECT 1 FROM pg_publication_tables
              WHERE pubname = 'supabase_realtime'
                AND schemaname = 'public'
                AND tablename = 'jobs'
            ) THEN
              EXECUTE 'ALTER PUBLICATION supabase_realtime ADD TABLE public.jobs';
            END IF;

            IF NOT EXISTS (
              SELECT 1 FROM pg_publication_tables
              WHERE pubname = 'supabase_realtime'
                AND schemaname = 'public'
                AND tablename = 'image_tasks'
            ) THEN
              EXECUTE 'ALTER PUBLICATION supabase_realtime ADD TABLE public.image_tasks';
            END IF;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'supabase_realtime') THEN
            IF EXISTS (
              SELECT 1 FROM pg_publication_tables
              WHERE pubname = 'supabase_realtime'
                AND schemaname = 'public'
                AND tablename = 'image_tasks'
            ) THEN
              EXECUTE 'ALTER PUBLICATION supabase_realtime DROP TABLE public.image_tasks';
            END IF;

            IF EXISTS (
              SELECT 1 FROM pg_publication_tables
              WHERE pubname = 'supabase_realtime'
                AND schemaname = 'public'
                AND tablename = 'jobs'
            ) THEN
              EXECUTE 'ALTER PUBLICATION supabase_realtime DROP TABLE public.jobs';
            END IF;
          END IF;
        END $$;
        """
    )
