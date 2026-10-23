-- ============================================================================
-- 08 - Seed: turbines (104, generated per site from sites.turbine_count)
-- Codes are <SITE_CODE>-NN (e.g. NSO-04). Model/status/output derived so the
-- fleet has a realistic spread of operating / maintenance / offline states.
-- ============================================================================
SELECT setseed(0.42);

INSERT INTO turbines (
    site_id, turbine_code, model, rated_power_kw, rotor_diameter_m, hub_height_m,
    cut_in_speed_ms, cut_out_speed_ms, rated_wind_speed_ms,
    installation_date, last_major_overhaul, warranty_expiry,
    status, current_power_output_kw, total_operating_hours, total_energy_produced_mwh
)
SELECT
    s.site_id,
    s.site_code || '-' || lpad(g::text, 2, '0')            AS turbine_code,
    m.model,
    m.rated                                                AS rated_power_kw,
    CASE m.model WHEN 'L' THEN 90 WHEN 'M' THEN 117 ELSE 145 END::decimal(5,2) AS rotor_diameter_m,
    CASE m.model WHEN 'L' THEN 80 WHEN 'M' THEN 95  ELSE 120 END::decimal(5,2) AS hub_height_m,
    3.0, 25.0, 12.0,
    (DATE '2016-01-01' + (random() * 1600)::int)           AS installation_date,
    (DATE '2024-01-01' + (random() * 400)::int)            AS last_major_overhaul,
    (DATE '2027-01-01' + (random() * 800)::int)            AS warranty_expiry,
    st.status,
    CASE WHEN st.status = 'operational'
         THEN round((m.rated * (0.20 + random() * 0.70))::numeric, 2)
         ELSE 0 END                                        AS current_power_output_kw,
    (8000 + random() * 60000)::int                         AS total_operating_hours,
    round((2000 + random() * 90000)::numeric, 2)           AS total_energy_produced_mwh
FROM sites s
CROSS JOIN LATERAL generate_series(1, s.turbine_count) AS g
CROSS JOIN LATERAL (
    SELECT mdl::turbine_model AS model,
           CASE mdl WHEN 'L' THEN 2200 WHEN 'M' THEN 3600 ELSE 5300 END AS rated
    FROM (SELECT CASE (g % 3) WHEN 0 THEN 'H' WHEN 1 THEN 'L' ELSE 'M' END AS mdl) x
) m
CROSS JOIN LATERAL (
    SELECT (CASE
                WHEN g % 17 = 0 THEN 'offline'
                WHEN g % 11 = 0 THEN 'maintenance'
                WHEN g % 29 = 0 THEN 'failed'
                ELSE 'operational'
            END)::turbine_status AS status
) st;
