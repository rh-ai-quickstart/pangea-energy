-- ============================================================================
-- 09 - Seed: time-series for sensor_readings, wind_data, ml_predictions
--
-- 90 days of hourly readings per turbine, generated from a physical chain
-- rather than independent random draws:
--
--     weather  ->  load  ->  temperature / vibration
--
-- process_temperature_c is a genuine function of ambient temperature and load
-- (ambient + 12 + 22 * load_fraction + noise). That relationship is what a
-- normal-behaviour model learns; without it every residual is noise and the
-- predictive panels have nothing real to detect.
--
-- Turbines in the "attention" set get a fault ramp over the final 45 days:
-- process temperature and vibration drift upward for the same load, which is
-- exactly the signature a residual model should catch.
-- ============================================================================
SELECT setseed(0.42);

-- Attention turbines (kept consistent with alerts/work-orders in seed 10):
--   NSO-04, MTP-07, CRG-03, DFL-05, MWP-11

-- ---------------------------------------------------------------------------
-- Sensor readings: hourly for the last 90 days, per turbine
-- ---------------------------------------------------------------------------
INSERT INTO sensor_readings (
    reading_time, turbine_id, air_temperature_c, process_temperature_c,
    rotational_speed_rpm, torque_nm, tool_wear_minutes,
    vibration_x_mms, vibration_y_mms, vibration_z_mms,
    power_output_kw, wind_speed_ms, wind_direction_deg,
    nacelle_position_deg, pitch_angle_deg, anomaly_score
)
SELECT
    ts,
    t.turbine_id,
    round(wx.air_temp::numeric, 2),
    -- the learnable relationship: ambient + idle rise + load rise + fault
    round((wx.air_temp + 12.0 + 22.0 * op.load_frac
           + 8.0 * fl.f + (random() - 0.5) * 2.4)::numeric, 2),
    op.rpm,
    round(op.torque::numeric, 2),
    ((k.hours_in * 2)::int % 240),
    round((0.90 + 1.10 * op.load_frac + 4.5 * fl.f + random() * 0.30)::numeric, 3),
    round((0.80 + 0.90 * op.load_frac + 3.4 * fl.f + random() * 0.25)::numeric, 3),
    round((0.60 + 0.70 * op.load_frac + 2.3 * fl.f + random() * 0.20)::numeric, 3),
    round(op.power::numeric, 2),
    round(wx.wind::numeric, 2),
    wx.wind_dir,
    (wx.wind_dir + (random() * 10 - 5)::int + 360) % 360,
    round((CASE WHEN wx.wind > 12 THEN least(25, (wx.wind - 12) * 2.6)
                ELSE random() * 1.5 END)::numeric, 2),
    least(1.0, round((0.03 + random() * 0.06 + fl.f * 0.85)::numeric, 4))
FROM turbines t
JOIN sites s ON s.site_id = t.site_id
CROSS JOIN LATERAL generate_series(
        date_trunc('hour', now()) - interval '90 days',
        date_trunc('hour', now()),
        interval '1 hour') AS ts
CROSS JOIN LATERAL (
    SELECT extract(hour FROM ts)::numeric            AS hr,
           extract(doy  FROM ts)::numeric            AS doy,
           extract(epoch FROM ts) / 3600.0           AS eh,
           (extract(epoch FROM ts)
            - extract(epoch FROM (date_trunc('hour', now()) - interval '90 days')))
           / 3600.0                                  AS hours_in
) k
CROSS JOIN LATERAL (
    SELECT
        -- seasonal swing between the site's own min and max, peaking mid-July,
        -- plus a daily cycle peaking mid-afternoon
        (s.min_temp_celsius + s.max_temp_celsius) / 2.0
          + (s.max_temp_celsius - s.min_temp_celsius) / 2.0
            * (-cos(2 * pi() * (k.doy - 15) / 365.0))
          + 3.5 * sin(2 * pi() * (k.hr - 9) / 24.0)
          + (random() - 0.5) * 3.0                   AS air_temp,
        -- synoptic systems every ~3 days, a weaker daily cycle, then noise
        greatest(0.2,
            s.avg_wind_speed_ms
              + 3.2 * sin(2 * pi() * k.eh / 72.0)
              + 1.1 * sin(2 * pi() * (k.hr - 15) / 24.0)
              + (random() - 0.5) * 2.4)              AS wind,
        ((180 + 120 * sin(2 * pi() * k.eh / 96.0) + random() * 40)::int % 360) AS wind_dir
) wx
CROSS JOIN LATERAL (
    SELECT CASE WHEN t.turbine_code IN ('NSO-04','MTP-07','CRG-03','DFL-05','MWP-11')
                     AND k.hours_in > 45 * 24
                THEN least(1.0, (k.hours_in - 45 * 24) / (45 * 24.0))
                ELSE 0 END AS f
) fl
CROSS JOIN LATERAL (
    SELECT op0.operating,
           op0.load_frac,
           CASE WHEN op0.operating THEN t.rated_power_kw * op0.load_frac ELSE 0 END AS power,
           CASE WHEN op0.operating
                THEN (600 + 900 * least(1.0, (wx.wind - 3) / 9.0))::int ELSE 0 END  AS rpm,
           CASE WHEN op0.operating AND wx.wind > 3
                THEN t.rated_power_kw * op0.load_frac * 9550.0
                     / greatest(1, (600 + 900 * least(1.0, (wx.wind - 3) / 9.0)))
                ELSE 0 END AS torque
    FROM (
        SELECT (t.status = 'operational' AND wx.wind >= 3 AND wx.wind < 25) AS operating,
               -- cubic power curve from cut-in 3 m/s to rated 12 m/s
               greatest(0, least(1.0, power(greatest(0, (wx.wind - 3)) / 9.0, 3))) AS load_frac
    ) op0
) op;

-- ---------------------------------------------------------------------------
-- Wind data: hourly for the last 90 days, per site (same weather model)
-- ---------------------------------------------------------------------------
INSERT INTO wind_data (
    measurement_time, site_id, wind_speed_ms, wind_gust_ms, wind_direction_deg,
    air_temperature_c, humidity_percent, pressure_hpa, precipitation_mm,
    visibility_km, data_source
)
SELECT
    ts,
    s.site_id,
    round(wx.wind::numeric, 2),
    round((wx.wind * (1.25 + random() * 0.35))::numeric, 2),
    wx.wind_dir,
    round(wx.air_temp::numeric, 2),
    (40 + random() * 55)::int,
    round((1013 - 12 * sin(2 * pi() * k.eh / 72.0) + random() * 8)::numeric, 2),
    round((CASE WHEN random() < 0.18 THEN random() * 3 ELSE 0 END)::numeric, 2),
    round((5 + random() * 35)::numeric, 2),
    'simulator'
FROM sites s
CROSS JOIN LATERAL generate_series(
        date_trunc('hour', now()) - interval '90 days',
        date_trunc('hour', now()),
        interval '1 hour') AS ts
CROSS JOIN LATERAL (
    SELECT extract(hour FROM ts)::numeric  AS hr,
           extract(doy  FROM ts)::numeric  AS doy,
           extract(epoch FROM ts) / 3600.0 AS eh
) k
CROSS JOIN LATERAL (
    SELECT
        (s.min_temp_celsius + s.max_temp_celsius) / 2.0
          + (s.max_temp_celsius - s.min_temp_celsius) / 2.0
            * (-cos(2 * pi() * (k.doy - 15) / 365.0))
          + 3.5 * sin(2 * pi() * (k.hr - 9) / 24.0)
          + (random() - 0.5) * 3.0                   AS air_temp,
        greatest(0.2,
            s.avg_wind_speed_ms
              + 3.2 * sin(2 * pi() * k.eh / 72.0)
              + 1.1 * sin(2 * pi() * (k.hr - 15) / 24.0)
              + (random() - 0.5) * 2.4)              AS wind,
        ((180 + 120 * sin(2 * pi() * k.eh / 96.0) + random() * 40)::int % 360) AS wind_dir
) wx;

-- ---------------------------------------------------------------------------
-- ML predictions: every 6 hours for the last 14 days, per turbine.
-- Placeholder rows so the predictive panels have something before the real
-- model is wired up; inputs are drawn from the matching sensor reading so they
-- agree with the time-series rather than contradicting it.
-- ---------------------------------------------------------------------------
INSERT INTO ml_predictions (
    prediction_time, turbine_id, model_name, model_version,
    input_air_temp_c, input_process_temp_c, input_rpm, input_torque_nm,
    input_tool_wear_min, input_model_type,
    failure_predicted, failure_probability, time_to_failure_hours,
    predicted_failure_mode, confidence_score, inference_latency_ms
)
SELECT
    sr.reading_time,
    sr.turbine_id,
    'pangea-pdm', 'v0.1',
    sr.air_temperature_c,
    sr.process_temperature_c,
    sr.rotational_speed_rpm,
    sr.torque_nm,
    sr.tool_wear_minutes,
    CASE t.model WHEN 'L' THEN 0 WHEN 'M' THEN 1 ELSE 2 END,
    (sr.anomaly_score > 0.5),
    sr.anomaly_score,
    CASE WHEN sr.anomaly_score > 0.5
         THEN greatest(6, (720 * (1 - sr.anomaly_score))::int) ELSE NULL END,
    CASE WHEN sr.anomaly_score > 0.5
         THEN (ARRAY['HDF','PWF','OSF','TWF'])[(1 + floor(random() * 4))::int] ELSE NULL END,
    round((0.70 + random() * 0.30)::numeric, 4),
    (8 + random() * 40)::int
FROM sensor_readings sr
JOIN turbines t ON t.turbine_id = sr.turbine_id
WHERE sr.reading_time >= date_trunc('hour', now()) - interval '14 days'
  AND extract(hour FROM sr.reading_time)::int % 6 = 0;

-- Populate the rollup materialized views from the freshly seeded data.
SELECT refresh_rollups();
