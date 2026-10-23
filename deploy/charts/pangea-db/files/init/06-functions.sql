-- ============================================================================
-- 06 - Functions
-- ============================================================================

-- ---------------------------------------------------------------------------
-- RAG similarity search over the knowledge base (used by the conversational AI)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION search_knowledge_base(
    query_embedding vector(1536),
    match_threshold float DEFAULT 0.7,
    match_count int DEFAULT 5
)
RETURNS TABLE (
    document_id int,
    title varchar,
    content text,
    similarity float
)
LANGUAGE sql STABLE
AS $$
    SELECT
        document_id,
        title,
        content,
        1 - (embedding <=> query_embedding) AS similarity
    FROM knowledge_base
    WHERE 1 - (embedding <=> query_embedding) > match_threshold
      AND is_active = TRUE
    ORDER BY embedding <=> query_embedding
    LIMIT match_count;
$$;

-- ---------------------------------------------------------------------------
-- Refresh the rollup materialized views.
-- Call on a schedule (e.g. hourly) from the API/analytics layer or a CronJob.
-- Uses CONCURRENTLY (non-locking) once a view is populated; falls back to a
-- plain REFRESH the first time, because CONCURRENTLY cannot run against a
-- materialized view that was created WITH NO DATA and never populated.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION refresh_rollups()
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    mv     text;
    is_pop boolean;
BEGIN
    FOREACH mv IN ARRAY ARRAY[
        'sensor_readings_hourly',
        'daily_prediction_summary',
        'wind_data_hourly'
    ]
    LOOP
        SELECT ispopulated INTO is_pop FROM pg_matviews WHERE matviewname = mv;
        IF is_pop THEN
            EXECUTE format('REFRESH MATERIALIZED VIEW CONCURRENTLY %I', mv);
        ELSE
            EXECUTE format('REFRESH MATERIALIZED VIEW %I', mv);
        END IF;
    END LOOP;
END;
$$;

-- ---------------------------------------------------------------------------
-- Shift-aware scheduling helpers
-- ---------------------------------------------------------------------------

-- The shift an operative is actually working on a given date, taking the
-- rota, one-off exception dates and long-term leave into account. Returns
-- 'off' when they are not available at all.
CREATE OR REPLACE FUNCTION operative_shift(p_operative_id INT, p_day DATE)
RETURNS shift_type
LANGUAGE sql STABLE
AS $$
    SELECT CASE
        WHEN o.on_leave_until IS NOT NULL AND o.on_leave_until >= p_day THEN 'off'::shift_type
        WHEN p_day = ANY(COALESCE(s.exception_dates, '{}'::date[]))     THEN 'off'::shift_type
        ELSE COALESCE(
            CASE EXTRACT(ISODOW FROM p_day)
                WHEN 1 THEN s.monday   WHEN 2 THEN s.tuesday
                WHEN 3 THEN s.wednesday WHEN 4 THEN s.thursday
                WHEN 5 THEN s.friday   WHEN 6 THEN s.saturday
                ELSE s.sunday
            END, 'off'::shift_type)
    END
    FROM maintenance_operatives o
    LEFT JOIN LATERAL (
        SELECT * FROM operative_schedules
        WHERE operative_id = o.operative_id AND effective_date <= p_day
          AND (end_date IS NULL OR end_date >= p_day)
        ORDER BY effective_date DESC LIMIT 1
    ) s ON TRUE
    WHERE o.operative_id = p_operative_id;
$$;

-- When a shift begins on a given date. NULL for 'off'; for 'on_call' there is
-- no fixed window so callouts are anchored to the start of the day shift.
CREATE OR REPLACE FUNCTION shift_start_at(p_shift shift_type, p_day DATE)
RETURNS TIMESTAMPTZ
LANGUAGE sql STABLE
AS $$
    SELECT CASE WHEN p_shift = 'off' THEN NULL
                ELSE p_day + make_interval(hours => COALESCE(
                    (SELECT start_hour FROM shift_definitions WHERE shift = p_shift),
                    (SELECT start_hour FROM shift_definitions WHERE shift = 'day')))
           END;
$$;

-- The first date on or after p_from when the operative is working, searching
-- at most two weeks ahead. NULL if they are off for the whole fortnight.
CREATE OR REPLACE FUNCTION next_working_day(p_operative_id INT, p_from DATE)
RETURNS DATE
LANGUAGE sql STABLE
AS $$
    SELECT d::date
    FROM generate_series(p_from, p_from + 13, interval '1 day') d
    WHERE operative_shift(p_operative_id, d::date) <> 'off'
    ORDER BY d LIMIT 1;
$$;

-- Re-flow every open assignment onto its operative's real shifts: each job
-- moves to the first day on or after its target date that the operative is
-- working, starts when that shift starts, and never overlaps the job before
-- it. Multi-day jobs occupy consecutive working days.
CREATE OR REPLACE FUNCTION reflow_assignments()
RETURNS INT
LANGUAGE plpgsql
AS $$
DECLARE
    r           RECORD;
    cur_op      INT := NULL;
    cursor_day  DATE;
    end_day     DATE;
    slot        TIMESTAMPTZ;
    span        INT;
    moved       INT := 0;
BEGIN
    FOR r IN
        SELECT work_order_id, assigned_operative_id, estimated_hours, scheduled_start
        FROM work_orders
        WHERE assigned_operative_id IS NOT NULL
          AND scheduled_start IS NOT NULL
          AND status NOT IN ('completed', 'cancelled')
        ORDER BY assigned_operative_id, scheduled_start
    LOOP
        IF cur_op IS DISTINCT FROM r.assigned_operative_id THEN
            cur_op := r.assigned_operative_id;
            cursor_day := r.scheduled_start::date;
        END IF;

        cursor_day := next_working_day(
            cur_op, GREATEST(r.scheduled_start::date, cursor_day));
        CONTINUE WHEN cursor_day IS NULL;

        slot := shift_start_at(operative_shift(cur_op, cursor_day), cursor_day);
        span := GREATEST(1, CEIL(COALESCE(r.estimated_hours, 8) / 8.0)::int);

        -- A multi-day job runs over consecutive *working* days, so it pauses
        -- over a rest day rather than counting it as progress.
        end_day := cursor_day;
        FOR i IN 2..span LOOP
            end_day := COALESCE(next_working_day(cur_op, end_day + 1), end_day + 1);
        END LOOP;

        UPDATE work_orders
           SET scheduled_start = slot,
               scheduled_end   = shift_start_at(operative_shift(cur_op, end_day), end_day)
                                 + interval '8 hours',
               updated_at      = now()
         WHERE work_order_id = r.work_order_id;

        cursor_day := end_day + 1;
        moved := moved + 1;
    END LOOP;
    RETURN moved;
END;
$$;

-- The working days a scheduled job occupies and how much of each shift it
-- takes. Anchored on the date the shift starts, so a night shift running past
-- midnight stays on the day it began.
CREATE OR REPLACE FUNCTION work_order_days(p_work_order_id INT)
RETURNS TABLE (day DATE, hours NUMERIC)
LANGUAGE plpgsql STABLE
AS $$
DECLARE
    r       RECORD;
    cap     NUMERIC;
    left_h  NUMERIC;
    d       DATE;
BEGIN
    SELECT assigned_operative_id AS op, scheduled_start::date AS start_day,
           GREATEST(COALESCE(estimated_hours, 8), 1) AS est
      INTO r
      FROM work_orders WHERE work_order_id = p_work_order_id;

    IF r.op IS NULL OR r.start_day IS NULL THEN
        RETURN;
    END IF;

    d := r.start_day;
    left_h := r.est;
    WHILE left_h > 0 AND d <= r.start_day + 30 LOOP
        cap := COALESCE((SELECT sd.booked_hours FROM shift_definitions sd
                          WHERE sd.shift = operative_shift(r.op, d)), 0);
        IF cap > 0 THEN                      -- rest days absorb nothing
            day   := d;
            hours := LEAST(left_h, cap);
            RETURN NEXT;
            left_h := left_h - hours;
        END IF;
        d := d + 1;
    END LOOP;
END;
$$;

-- Day-by-day availability for the active roster: the shift each person works,
-- why they are off, and how much of it is committed. One resolution of the
-- rota that the API, the booking form and the assistant all read.
CREATE OR REPLACE FUNCTION operative_availability(p_from DATE, p_days INT)
RETURNS TABLE (operative_id INT, day DATE, shift TEXT, on_leave BOOLEAN,
               is_exception BOOLEAN, capacity_hours NUMERIC, booked_hours NUMERIC)
LANGUAGE sql STABLE
AS $$
    WITH job_days AS (
        SELECT w.assigned_operative_id AS op, wd.day, SUM(wd.hours) AS hours
        FROM work_orders w
        CROSS JOIN LATERAL work_order_days(w.work_order_id) wd
        WHERE w.assigned_operative_id IS NOT NULL
          AND w.scheduled_start IS NOT NULL
          AND w.status NOT IN ('completed', 'cancelled')
        GROUP BY 1, 2
    )
    SELECT o.operative_id,
           g.d::date,
           operative_shift(o.operative_id, g.d::date)::text,
           o.on_leave_until IS NOT NULL AND o.on_leave_until >= g.d::date,
           g.d::date = ANY(COALESCE(s.exception_dates, '{}'::date[])),
           COALESCE(sd.booked_hours, 0)::numeric,
           COALESCE(j.hours, 0)
    FROM maintenance_operatives o
    CROSS JOIN generate_series(p_from, p_from + (p_days - 1), interval '1 day') g(d)
    LEFT JOIN LATERAL (
        SELECT * FROM operative_schedules
        WHERE operative_id = o.operative_id AND effective_date <= g.d::date
          AND (end_date IS NULL OR end_date >= g.d::date)
        ORDER BY effective_date DESC LIMIT 1
    ) s ON TRUE
    LEFT JOIN shift_definitions sd
           ON sd.shift = operative_shift(o.operative_id, g.d::date)
    LEFT JOIN job_days j
           ON j.op = o.operative_id AND j.day = g.d::date
    WHERE o.is_active
    ORDER BY o.operative_id, g.d;
$$;

-- Pull the demo timeline forward so it never goes stale. Anything open and
-- dated in the past moves on by whole weeks, which keeps it on the same
-- weekday and so on the same side of each operative's rota; future work is
-- left alone. Idempotent — a second run in the same day changes nothing.
CREATE OR REPLACE FUNCTION rebase_demo_dates()
RETURNS TABLE (moved_orders INT, moved_exceptions INT, reflowed INT)
LANGUAGE plpgsql
AS $$
DECLARE
    orders INT := 0;
    excs   INT := 0;
BEGIN
    WITH shifted AS (
        UPDATE work_orders w
           SET scheduled_start = w.scheduled_start
                 + make_interval(days => (CEIL((CURRENT_DATE - w.scheduled_start::date)
                                               / 7.0) * 7)::int),
               scheduled_end   = w.scheduled_end
                 + make_interval(days => (CEIL((CURRENT_DATE - w.scheduled_start::date)
                                               / 7.0) * 7)::int),
               updated_at      = NOW()
         WHERE w.status NOT IN ('completed', 'cancelled')
           AND w.scheduled_start IS NOT NULL
           AND w.scheduled_start::date < CURRENT_DATE
        RETURNING 1
    )
    SELECT count(*)::int INTO orders FROM shifted;

    -- Training days and the like, so the calendar keeps showing exceptions.
    WITH shifted AS (
        UPDATE operative_schedules s
           SET exception_dates = ARRAY(
                   SELECT CASE WHEN e < CURRENT_DATE
                               THEN e + (CEIL((CURRENT_DATE - e) / 7.0) * 7)::int
                               ELSE e END
                   FROM unnest(s.exception_dates) e)
         WHERE s.exception_dates IS NOT NULL
           AND EXISTS (SELECT 1 FROM unnest(s.exception_dates) e
                        WHERE e < CURRENT_DATE)
        RETURNING 1
    )
    SELECT count(*)::int INTO excs FROM shifted;

    RETURN QUERY SELECT orders, excs, reflow_assignments();
END;
$$;
