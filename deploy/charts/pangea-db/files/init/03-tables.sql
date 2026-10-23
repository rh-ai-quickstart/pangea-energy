-- ============================================================================
-- 03 - Tables
-- Time-series tables (sensor_readings, ml_predictions, wind_data) are plain
-- Postgres tables keyed on (time, id). No TimescaleDB hypertables.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. Sites
-- ---------------------------------------------------------------------------
CREATE TABLE sites (
    site_id            SERIAL PRIMARY KEY,
    site_name          VARCHAR(100) NOT NULL UNIQUE,
    site_code          VARCHAR(10)  NOT NULL UNIQUE,
    terrain            site_terrain NOT NULL,
    energy_type        energy_kind  NOT NULL DEFAULT 'wind',  -- wind | solar | wave
    latitude           DECIMAL(9,6),
    longitude          DECIMAL(9,6),
    elevation_meters   INT,
    turbine_count      INT NOT NULL,
    capacity_kw        INT,           -- rated plant capacity; used for non-turbine (solar/wave) sites
    commissioned_date  DATE,

    avg_wind_speed_ms  DECIMAL(5,2),
    min_temp_celsius   INT,
    max_temp_celsius   INT,

    access_difficulty  VARCHAR(50),   -- 'easy' | 'moderate' | 'difficult' | 'weather-dependent'
    nearest_city       VARCHAR(100),
    country            VARCHAR(60),
    travel_time_hours  DECIMAL(4,2),

    created_at         TIMESTAMPTZ DEFAULT NOW(),
    updated_at         TIMESTAMPTZ DEFAULT NOW(),
    is_active          BOOLEAN DEFAULT TRUE
);

-- ---------------------------------------------------------------------------
-- 2. Turbines
-- ---------------------------------------------------------------------------
CREATE TABLE turbines (
    turbine_id         SERIAL PRIMARY KEY,
    site_id            INT NOT NULL REFERENCES sites(site_id) ON DELETE CASCADE,
    turbine_code       VARCHAR(20) NOT NULL UNIQUE,   -- e.g. 'MT-01', 'OS-15'
    model              turbine_model NOT NULL,

    rated_power_kw       INT NOT NULL,
    rotor_diameter_m     DECIMAL(5,2),
    hub_height_m         DECIMAL(5,2),
    cut_in_speed_ms      DECIMAL(4,2) DEFAULT 3.0,
    cut_out_speed_ms     DECIMAL(4,2) DEFAULT 25.0,
    rated_wind_speed_ms  DECIMAL(4,2) DEFAULT 12.0,

    installation_date    DATE,
    last_major_overhaul  DATE,
    warranty_expiry      DATE,

    status                     turbine_status DEFAULT 'operational',
    current_power_output_kw    DECIMAL(8,2) DEFAULT 0,
    total_operating_hours      INT DEFAULT 0,
    total_energy_produced_mwh  DECIMAL(12,2) DEFAULT 0,

    created_at         TIMESTAMPTZ DEFAULT NOW(),
    updated_at         TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 3. Sensor readings (time-series, plain table)
-- ---------------------------------------------------------------------------
CREATE TABLE sensor_readings (
    reading_time        TIMESTAMPTZ NOT NULL,
    turbine_id          INT NOT NULL REFERENCES turbines(turbine_id) ON DELETE CASCADE,

    -- Core sensor data (source CSV is in Kelvin; stored here in Celsius)
    air_temperature_c     DECIMAL(6,2),
    process_temperature_c DECIMAL(6,2),  -- gearbox / generator temp
    rotational_speed_rpm  INT,
    torque_nm             DECIMAL(8,2),
    tool_wear_minutes     INT,

    -- Additional sensors
    rotor_bearing_temp_c     DECIMAL(6,2),
    gearbox_oil_temp_c       DECIMAL(6,2),
    generator_bearing_temp_c DECIMAL(6,2),
    transformer_temp_c       DECIMAL(6,2),
    source_asset             TEXT,          -- CARE asset this was replayed from

    vibration_x_mms   DECIMAL(6,3),
    vibration_y_mms   DECIMAL(6,3),
    vibration_z_mms   DECIMAL(6,3),
    power_output_kw   DECIMAL(8,2),
    wind_speed_ms     DECIMAL(5,2),
    wind_direction_deg INT,
    nacelle_position_deg INT,
    pitch_angle_deg   DECIMAL(5,2),

    -- Derived
    anomaly_score     DECIMAL(5,4),      -- 0.0 - 1.0

    PRIMARY KEY (reading_time, turbine_id)
);

-- ---------------------------------------------------------------------------
-- 4. Maintenance operatives
-- ---------------------------------------------------------------------------
CREATE TABLE maintenance_operatives (
    operative_id     SERIAL PRIMARY KEY,
    employee_code    VARCHAR(20) NOT NULL UNIQUE,
    first_name       VARCHAR(50) NOT NULL,
    last_name        VARCHAR(50) NOT NULL,
    email            VARCHAR(100) UNIQUE,
    phone            VARCHAR(20),

    skill_level      skill_level NOT NULL,
    certifications   certification_type[] NOT NULL,
    specializations  TEXT[],

    base_location            VARCHAR(100),
    max_travel_distance_km   INT DEFAULT 500,
    offshore_certified       BOOLEAN DEFAULT FALSE,
    rope_access_certified    BOOLEAN DEFAULT FALSE,
    high_altitude_certified  BOOLEAN DEFAULT FALSE,

    hire_date            DATE NOT NULL,
    hourly_rate          DECIMAL(8,2),
    overtime_multiplier  DECIMAL(3,2) DEFAULT 1.5,

    is_active        BOOLEAN DEFAULT TRUE,
    on_leave_until   DATE,

    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 5. Operative schedules
-- ---------------------------------------------------------------------------

-- Clock hours for each shift. One row per shift_type, so the rota, the API and
-- the dashboard all agree on when a shift actually runs. 'night' wraps past
-- midnight (start_hour > end_hour); 'on_call' and 'off' have no fixed window.
CREATE TABLE shift_definitions (
    shift        shift_type PRIMARY KEY,
    start_hour   INT,
    end_hour     INT,
    booked_hours INT NOT NULL DEFAULT 0,   -- work a shift can absorb in a day
    description  TEXT
);

INSERT INTO shift_definitions (shift, start_hour, end_hour, booked_hours, description) VALUES
    ('day',     7, 19, 8, 'Day shift, 07:00-19:00'),
    ('night',  19,  7, 8, 'Night shift, 19:00-07:00'),
    ('on_call', NULL, NULL, 8, 'Callout cover, no fixed window'),
    ('off',    NULL, NULL, 0, 'Not working');

CREATE TABLE operative_schedules (
    schedule_id      SERIAL PRIMARY KEY,
    operative_id     INT NOT NULL REFERENCES maintenance_operatives(operative_id) ON DELETE CASCADE,

    effective_date   DATE NOT NULL,
    end_date         DATE,             -- NULL = ongoing

    monday    shift_type DEFAULT 'off',
    tuesday   shift_type DEFAULT 'off',
    wednesday shift_type DEFAULT 'off',
    thursday  shift_type DEFAULT 'off',
    friday    shift_type DEFAULT 'off',
    saturday  shift_type DEFAULT 'off',
    sunday    shift_type DEFAULT 'off',

    exception_dates   DATE[],
    exception_reason  TEXT,

    created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 6. Work orders
-- ---------------------------------------------------------------------------
CREATE TABLE work_orders (
    work_order_id      SERIAL PRIMARY KEY,
    work_order_number  VARCHAR(20) NOT NULL UNIQUE,  -- e.g. 'WO-2026-001234'

    turbine_id    INT NOT NULL REFERENCES turbines(turbine_id) ON DELETE CASCADE,
    work_type     work_order_type NOT NULL,
    priority      work_order_priority NOT NULL,
    status        work_order_status DEFAULT 'scheduled',

    title         VARCHAR(200) NOT NULL,
    description   TEXT,
    failure_mode  VARCHAR(50),   -- TWF, HDF, PWF, OSF, RNF, or NULL

    assigned_operative_id  INT REFERENCES maintenance_operatives(operative_id),
    assigned_date          TIMESTAMPTZ,

    scheduled_start  TIMESTAMPTZ,
    scheduled_end    TIMESTAMPTZ,
    actual_start     TIMESTAMPTZ,
    actual_end       TIMESTAMPTZ,
    estimated_hours  DECIMAL(5,2),
    actual_hours     DECIMAL(5,2),

    predicted_failure_probability  DECIMAL(5,4),
    prediction_timestamp           TIMESTAMPTZ,
    ml_triggered                   BOOLEAN DEFAULT FALSE,

    requires_low_wind    BOOLEAN DEFAULT TRUE,
    max_wind_speed_ms    DECIMAL(4,2) DEFAULT 8.0,

    completion_notes     TEXT,
    work_performed       TEXT,
    root_cause_analysis  TEXT,

    labor_cost   DECIMAL(10,2),
    parts_cost   DECIMAL(10,2),
    total_cost   DECIMAL(10,2),

    created_by   VARCHAR(100),
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 7. Parts inventory
-- ---------------------------------------------------------------------------
CREATE TABLE parts_inventory (
    part_id       SERIAL PRIMARY KEY,
    part_number   VARCHAR(50) NOT NULL UNIQUE,
    part_name     VARCHAR(200) NOT NULL,
    category      part_category NOT NULL,

    compatible_models  turbine_model[],

    manufacturer  VARCHAR(100),
    description   TEXT,

    quantity_on_hand   INT NOT NULL DEFAULT 0,
    quantity_reserved  INT NOT NULL DEFAULT 0,
    reorder_point      INT NOT NULL DEFAULT 2,
    reorder_quantity   INT NOT NULL DEFAULT 5,

    unit_cost       DECIMAL(10,2),
    lead_time_days  INT,

    warehouse_location  VARCHAR(100),
    shelf_life_days     INT,

    is_critical   BOOLEAN DEFAULT FALSE,

    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 8. Parts requirements (parts needed per work order)
-- ---------------------------------------------------------------------------
CREATE TABLE parts_requirements (
    requirement_id     SERIAL PRIMARY KEY,
    work_order_id      INT NOT NULL REFERENCES work_orders(work_order_id) ON DELETE CASCADE,
    part_id            INT NOT NULL REFERENCES parts_inventory(part_id),

    quantity_required  INT NOT NULL,
    quantity_allocated INT DEFAULT 0,
    quantity_used      INT DEFAULT 0,

    notes              TEXT,
    created_at         TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (work_order_id, part_id)
);

-- ---------------------------------------------------------------------------
-- 9. ML predictions (time-series, plain table)
-- ---------------------------------------------------------------------------
CREATE TABLE ml_predictions (
    prediction_time   TIMESTAMPTZ NOT NULL,
    turbine_id        INT NOT NULL REFERENCES turbines(turbine_id) ON DELETE CASCADE,

    model_name        VARCHAR(100) NOT NULL,
    model_version     VARCHAR(50),

    -- Input features (for reproducibility)
    input_air_temp_c      DECIMAL(6,2),
    input_process_temp_c  DECIMAL(6,2),
    input_rpm             INT,
    input_torque_nm       DECIMAL(8,2),
    input_tool_wear_min   INT,
    input_model_type      INT,          -- 0=L, 1=M, 2=H

    failure_predicted        BOOLEAN,
    failure_probability      DECIMAL(5,4),
    time_to_failure_hours    INT,
    predicted_failure_mode   VARCHAR(50),
    confidence_score         DECIMAL(5,4),

    inference_latency_ms     INT,

    PRIMARY KEY (prediction_time, turbine_id)
);

-- ---------------------------------------------------------------------------
-- 10. Wind data (time-series, plain table)
-- ---------------------------------------------------------------------------
CREATE TABLE wind_data (
    measurement_time  TIMESTAMPTZ NOT NULL,
    site_id           INT NOT NULL REFERENCES sites(site_id) ON DELETE CASCADE,

    wind_speed_ms     DECIMAL(5,2) NOT NULL,
    wind_gust_ms      DECIMAL(5,2),
    wind_direction_deg INT,           -- 0-359

    air_temperature_c DECIMAL(5,2),
    humidity_percent  INT,
    pressure_hpa      DECIMAL(6,2),
    precipitation_mm  DECIMAL(5,2),
    visibility_km     DECIMAL(5,2),

    data_source       VARCHAR(50),    -- 'api' | 'simulator' | 'on_site_sensor'

    PRIMARY KEY (measurement_time, site_id)
);

-- ---------------------------------------------------------------------------
-- 11. Alerts
-- ---------------------------------------------------------------------------
CREATE TABLE alerts (
    alert_id      SERIAL PRIMARY KEY,
    alert_time    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    severity      alert_severity NOT NULL,
    category      alert_category NOT NULL,
    status        alert_status DEFAULT 'active',

    turbine_id    INT REFERENCES turbines(turbine_id) ON DELETE CASCADE,
    site_id       INT REFERENCES sites(site_id) ON DELETE CASCADE,
    work_order_id INT REFERENCES work_orders(work_order_id),

    title              VARCHAR(200) NOT NULL,
    message            TEXT NOT NULL,
    recommended_action TEXT,

    acknowledged_by    VARCHAR(100),
    acknowledged_at    TIMESTAMPTZ,
    resolution_notes   TEXT,
    resolved_at        TIMESTAMPTZ,

    created_at         TIMESTAMPTZ DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 12. Knowledge base (RAG for conversational AI)
-- ---------------------------------------------------------------------------
CREATE TABLE knowledge_base (
    document_id     SERIAL PRIMARY KEY,
    document_name   VARCHAR(200) NOT NULL,
    document_type   VARCHAR(50),   -- 'procedure' | 'troubleshooting' | 'safety' | 'technical_spec'

    title    VARCHAR(200) NOT NULL,
    content  TEXT NOT NULL,
    summary  TEXT,

    tags            TEXT[],
    turbine_models  turbine_model[],
    applicable_sites INT[],        -- site_ids (kept as a plain array for flexibility)

    -- Vector embedding for RAG. Dimension depends on the embedding model served
    -- by OpenShift AI; 1536 matches common embedding sizes. Adjust if needed.
    embedding vector(1536),

    version       INT DEFAULT 1,
    last_updated  TIMESTAMPTZ DEFAULT NOW(),
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    is_active     BOOLEAN DEFAULT TRUE
);

-- ---------------------------------------------------------------------------
-- 13. Agent actions
-- ---------------------------------------------------------------------------

-- Audit trail for writes the assistant proposes. It never writes to
-- work_orders itself; only an explicit confirmation turns a proposal into one.
CREATE TABLE agent_actions (
    action_id    SERIAL PRIMARY KEY,
    session_id   TEXT,
    action       TEXT NOT NULL,             -- e.g. 'schedule_work'
    params       JSONB NOT NULL,            -- resolved arguments, not free text
    message      TEXT,                      -- what the user typed
    status       agent_action_status NOT NULL DEFAULT 'proposed',
    result       JSONB,                     -- work order created, or the error
    proposed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at  TIMESTAMPTZ
);

CREATE INDEX idx_agent_actions_status ON agent_actions(status, proposed_at DESC);
