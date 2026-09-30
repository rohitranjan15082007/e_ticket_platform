"""protect Phase 6 draw evidence and lifecycle transitions in PostgreSQL

Revision ID: 0011_phase6_draw_integrity_guards
Revises: 0010_phase6_draw_results
Create Date: 2026-09-24
"""

from alembic import op


revision = "0011_phase6_draw_integrity_guards"
down_revision = "0010_phase6_draw_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add database-side evidence immutability without constraining SQLite tests."""

    if op.get_bind().dialect.name == "postgresql":
        _install_postgresql_relationship_guards()
        _install_postgresql_draw_guards()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _remove_postgresql_draw_guards()
        _remove_postgresql_relationship_guards()


def _install_postgresql_relationship_guards() -> None:
    """Make draw-scoped child records refer to evidence from the same draw."""

    op.create_unique_constraint("uq_winner_draw_id", "winners", ["draw_id", "id"])
    op.create_foreign_key(
        "fk_winner_draw_entry",
        "winners",
        "draw_entries",
        ["draw_id", "ticket_id"],
        ["draw_id", "ticket_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_prize_award_draw_winner",
        "prize_awards",
        "winners",
        ["draw_id", "winner_id"],
        ["draw_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_draw_notification_draw_winner",
        "draw_notifications",
        "winners",
        ["draw_id", "winner_id"],
        ["draw_id", "id"],
        ondelete="RESTRICT",
    )


def _remove_postgresql_relationship_guards() -> None:
    op.drop_constraint("fk_draw_notification_draw_winner", "draw_notifications", type_="foreignkey")
    op.drop_constraint("fk_prize_award_draw_winner", "prize_awards", type_="foreignkey")
    op.drop_constraint("fk_winner_draw_entry", "winners", type_="foreignkey")
    op.drop_constraint("uq_winner_draw_id", "winners", type_="unique")


def _install_postgresql_draw_guards() -> None:
    op.execute(
        """
        CREATE FUNCTION ensure_series_open_has_draw_commitment()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.status::text = 'OPEN'
               AND OLD.status::text <> 'OPEN'
               AND NOT EXISTS (
                   SELECT 1
                   FROM draws
                   WHERE draws.series_id = NEW.id
                     AND draws.status::text = 'COMMITTED'
               )
            THEN
                RAISE EXCEPTION 'ticket series cannot open without a draw commitment'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION prohibit_draw_evidence_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '55000';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_draw_initial_state()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.status::text <> 'COMMITTED'
               OR NEW.eligible_ticket_count <> 0
               OR NEW.eligible_tickets_digest IS NOT NULL
               OR NEW.prize_snapshot IS NOT NULL
               OR NEW.result_digest IS NOT NULL
               OR NEW.seed_reveal IS NOT NULL
               OR NEW.closed_by_user_id IS NOT NULL
               OR NEW.closed_at IS NOT NULL
               OR NEW.drawn_by_user_id IS NOT NULL
               OR NEW.drawn_at IS NOT NULL
               OR NEW.prizes_posted_by_user_id IS NOT NULL
               OR NEW.prizes_posted_at IS NOT NULL
               OR NEW.published_by_user_id IS NOT NULL
               OR NEW.published_at IS NOT NULL
               OR NEW.seed_commitment !~ '^[0-9a-f]{64}$'
            THEN
                RAISE EXCEPTION 'draw must start as an empty COMMITTED evidence record'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION enforce_draw_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.series_id IS DISTINCT FROM OLD.series_id
               OR NEW.algorithm_version IS DISTINCT FROM OLD.algorithm_version
               OR NEW.seed_commitment IS DISTINCT FROM OLD.seed_commitment
               OR NEW.committed_by_user_id IS DISTINCT FROM OLD.committed_by_user_id
               OR NEW.committed_at IS DISTINCT FROM OLD.committed_at
            THEN
                RAISE EXCEPTION 'immutable draw evidence was modified' USING ERRCODE = '55000';
            END IF;

            IF OLD.status::text = 'COMMITTED' THEN
                IF NEW.status::text <> 'SALES_CLOSED'
                   OR NEW.eligible_ticket_count <= 0
                   OR NEW.eligible_tickets_digest IS NULL
                   OR NEW.eligible_tickets_digest !~ '^[0-9a-f]{64}$'
                   OR NEW.prize_snapshot IS NULL
                   OR NEW.closed_by_user_id IS NULL
                   OR NEW.closed_at IS NULL
                   OR NEW.seed_reveal IS NOT NULL
                   OR NEW.result_digest IS NOT NULL
                   OR NEW.drawn_by_user_id IS NOT NULL
                   OR NEW.drawn_at IS NOT NULL
                   OR NEW.prizes_posted_by_user_id IS NOT NULL
                   OR NEW.prizes_posted_at IS NOT NULL
                   OR NEW.published_by_user_id IS NOT NULL
                   OR NEW.published_at IS NOT NULL
                THEN
                    RAISE EXCEPTION 'invalid COMMITTED to SALES_CLOSED draw transition'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.status::text = 'SALES_CLOSED' THEN
                IF NEW.status::text <> 'DRAWN'
                   OR NEW.eligible_ticket_count IS DISTINCT FROM OLD.eligible_ticket_count
                   OR NEW.eligible_tickets_digest IS DISTINCT FROM OLD.eligible_tickets_digest
                   OR NEW.prize_snapshot::text IS DISTINCT FROM OLD.prize_snapshot::text
                   OR NEW.closed_by_user_id IS DISTINCT FROM OLD.closed_by_user_id
                   OR NEW.closed_at IS DISTINCT FROM OLD.closed_at
                   OR NEW.seed_reveal IS NULL
                   OR NEW.result_digest IS NULL
                   OR NEW.result_digest !~ '^[0-9a-f]{64}$'
                   OR NEW.drawn_by_user_id IS NULL
                   OR NEW.drawn_at IS NULL
                   OR NEW.prizes_posted_by_user_id IS NOT NULL
                   OR NEW.prizes_posted_at IS NOT NULL
                   OR NEW.published_by_user_id IS NOT NULL
                   OR NEW.published_at IS NOT NULL
                THEN
                    RAISE EXCEPTION 'invalid SALES_CLOSED to DRAWN draw transition'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.status::text = 'DRAWN' THEN
                IF NEW.status::text <> 'PRIZES_POSTED'
                   OR NEW.eligible_ticket_count IS DISTINCT FROM OLD.eligible_ticket_count
                   OR NEW.eligible_tickets_digest IS DISTINCT FROM OLD.eligible_tickets_digest
                   OR NEW.prize_snapshot::text IS DISTINCT FROM OLD.prize_snapshot::text
                   OR NEW.closed_by_user_id IS DISTINCT FROM OLD.closed_by_user_id
                   OR NEW.closed_at IS DISTINCT FROM OLD.closed_at
                   OR NEW.seed_reveal IS DISTINCT FROM OLD.seed_reveal
                   OR NEW.result_digest IS DISTINCT FROM OLD.result_digest
                   OR NEW.drawn_by_user_id IS DISTINCT FROM OLD.drawn_by_user_id
                   OR NEW.drawn_at IS DISTINCT FROM OLD.drawn_at
                   OR NEW.prizes_posted_by_user_id IS NULL
                   OR NEW.prizes_posted_at IS NULL
                   OR NEW.published_by_user_id IS NOT NULL
                   OR NEW.published_at IS NOT NULL
                THEN
                    RAISE EXCEPTION 'invalid DRAWN to PRIZES_POSTED draw transition'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.status::text = 'PRIZES_POSTED' THEN
                IF NEW.status::text <> 'RESULT_PUBLISHED'
                   OR NEW.eligible_ticket_count IS DISTINCT FROM OLD.eligible_ticket_count
                   OR NEW.eligible_tickets_digest IS DISTINCT FROM OLD.eligible_tickets_digest
                   OR NEW.prize_snapshot::text IS DISTINCT FROM OLD.prize_snapshot::text
                   OR NEW.closed_by_user_id IS DISTINCT FROM OLD.closed_by_user_id
                   OR NEW.closed_at IS DISTINCT FROM OLD.closed_at
                   OR NEW.seed_reveal IS DISTINCT FROM OLD.seed_reveal
                   OR NEW.result_digest IS DISTINCT FROM OLD.result_digest
                   OR NEW.drawn_by_user_id IS DISTINCT FROM OLD.drawn_by_user_id
                   OR NEW.drawn_at IS DISTINCT FROM OLD.drawn_at
                   OR NEW.prizes_posted_by_user_id IS DISTINCT FROM OLD.prizes_posted_by_user_id
                   OR NEW.prizes_posted_at IS DISTINCT FROM OLD.prizes_posted_at
                   OR NEW.published_by_user_id IS NULL
                   OR NEW.published_at IS NULL
                THEN
                    RAISE EXCEPTION 'invalid PRIZES_POSTED to RESULT_PUBLISHED draw transition'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;

            RAISE EXCEPTION 'terminal or unknown draw state cannot be modified' USING ERRCODE = '55000';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER ticket_series_open_requires_draw_commitment
        BEFORE UPDATE OF status ON ticket_series
        FOR EACH ROW EXECUTE FUNCTION ensure_series_open_has_draw_commitment();
        """
    )
    op.execute(
        """
        CREATE TRIGGER draws_validate_initial_state
        BEFORE INSERT ON draws
        FOR EACH ROW EXECUTE FUNCTION validate_draw_initial_state();
        """
    )
    op.execute(
        """
        CREATE TRIGGER draws_controlled_transition
        BEFORE UPDATE ON draws
        FOR EACH ROW EXECUTE FUNCTION enforce_draw_transition();
        """
    )
    op.execute(
        """
        CREATE TRIGGER draws_append_only_delete
        BEFORE DELETE ON draws
        FOR EACH ROW EXECUTE FUNCTION prohibit_draw_evidence_mutation();
        """
    )
    for table_name in ("draw_entries", "winners", "prize_awards", "draw_notifications"):
        op.execute(
            f"""
            CREATE TRIGGER {table_name}_append_only
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION prohibit_draw_evidence_mutation();
            """
        )


def _remove_postgresql_draw_guards() -> None:
    for table_name in ("draw_entries", "winners", "prize_awards", "draw_notifications"):
        op.execute(f"DROP TRIGGER {table_name}_append_only ON {table_name}")
    op.execute("DROP TRIGGER draws_append_only_delete ON draws")
    op.execute("DROP TRIGGER draws_controlled_transition ON draws")
    op.execute("DROP TRIGGER draws_validate_initial_state ON draws")
    op.execute("DROP TRIGGER ticket_series_open_requires_draw_commitment ON ticket_series")
    op.execute("DROP FUNCTION enforce_draw_transition()")
    op.execute("DROP FUNCTION validate_draw_initial_state()")
    op.execute("DROP FUNCTION prohibit_draw_evidence_mutation()")
    op.execute("DROP FUNCTION ensure_series_open_has_draw_commitment()")
