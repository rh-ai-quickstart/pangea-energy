-- ============================================================================
-- 05 - Views and rollups
-- Regular views for operational queries, and MATERIALIZED VIEWs replacing the
-- TimescaleDB continuous aggregates (refreshed on a schedule; see
-- refresh_rollups() in 06-functions.sql).
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Current operative availability (latest active schedule per operative)
-- ---------------------------------------------------------------------------
CREATE VIEW current_operative_availability AS
SELECT
    o.operative_id,
    o.employee_code,
    o.first_name || ' ' || o.last_name AS full_name,
    o.skill_level,
    o.certifications,
    o.base_location,
    o.offshore_certified,
    o.is_active,
    o.on_leave_until,
    s.monday, s.tuesday, s.wednesday, s.thursday, s.friday, s.saturday, s.sunday,
    s.exception_dates
FROM maintenance_operatives o
LEFT JOIN LATERAL (
    SELECT * FROM operative_schedules
    WHERE operative_id = o.operative_id
      AND effective_date <= CURRENT_DATE
      AND (end_date IS NULL OR end_date >= CURRENT_DATE)
    ORDER BY effective_date DESC
    LIMIT 1
) s ON TRUE
WHERE o.is_active = TRUE
  AND (o.on_leave_until IS NULL OR o.on_leave_until < CURRENT_DATE);

-- ---------------------------------------------------------------------------
-- Parts needing reorder
-- ---------------------------------------------------------------------------
CREATE VIEW parts_to_reorder AS
SELECT
    part_id, part_number, part_name, category,
    quantity_on_hand, quantity_reserved, reorder_point, reorder_quantity,
    (quantity_on_hand - quantity_reserved) AS available_quantity,
    unit_cost, lead_time_days, is_critical
FROM parts_inventory
WHERE (quantity_on_hand - quantity_reserved) <= reorder_point;

-- ---------------------------------------------------------------------------
-- Hourly sensor rollup (replaces TimescaleDB continuous aggregate)
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW sensor_readings_hourly AS
SELECT
    date_trunc('hour', reading_time) AS hour,
    turbine_id,
    AVG(air_temperature_c)     AS avg_air_temp_c,
    AVG(process_temperature_c) AS avg_process_temp_c,
    AVG(rotational_speed_rpm)  AS avg_rpm,
    AVG(torque_nm)             AS avg_torque_nm,
    AVG(power_output_kw)       AS avg_power_kw,
    MAX(vibration_x_mms)       AS max_vibration_x,
    MAX(vibration_y_mms)       AS max_vibration_y,
    MAX(vibration_z_mms)       AS max_vibration_z,
    COUNT(*)                   AS reading_count
FROM sensor_readings
GROUP BY 1, 2
WITH NO DATA;

-- Unique index enables REFRESH MATERIALIZED VIEW CONCURRENTLY.
CREATE UNIQUE INDEX idx_sensor_hourly_pk ON sensor_readings_hourly(hour, turbine_id);

-- ---------------------------------------------------------------------------
-- Daily prediction summary (replaces TimescaleDB continuous aggregate)
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW daily_prediction_summary AS
SELECT
    date_trunc('day', prediction_time) AS day,
    turbine_id,
    COUNT(*)                                          AS prediction_count,
    SUM(CASE WHEN failure_predicted THEN 1 ELSE 0 END) AS failure_predictions,
    AVG(failure_probability)                          AS avg_failure_probability,
    MAX(failure_probability)                          AS max_failure_probability,
    AVG(inference_latency_ms)                         AS avg_latency_ms
FROM ml_predictions
GROUP BY 1, 2
WITH NO DATA;

CREATE UNIQUE INDEX idx_daily_pred_pk ON daily_prediction_summary(day, turbine_id);

-- ---------------------------------------------------------------------------
-- Hourly wind rollup (replaces TimescaleDB continuous aggregate)
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW wind_data_hourly AS
SELECT
    date_trunc('hour', measurement_time) AS hour,
    site_id,
    AVG(wind_speed_ms)     AS avg_wind_speed_ms,
    MAX(wind_gust_ms)      AS max_gust_ms,
    AVG(air_temperature_c) AS avg_temp_c,
    COUNT(*)               AS measurement_count
FROM wind_data
GROUP BY 1, 2
WITH NO DATA;

CREATE UNIQUE INDEX idx_wind_hourly_pk ON wind_data_hourly(hour, site_id);
