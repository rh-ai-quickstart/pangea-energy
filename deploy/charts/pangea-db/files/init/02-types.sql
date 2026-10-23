-- ============================================================================
-- 02 - Enum types
-- ============================================================================

-- Sites / turbines
CREATE TYPE site_terrain    AS ENUM ('mountain', 'offshore', 'plains', 'coastal', 'desert');
CREATE TYPE energy_kind     AS ENUM ('wind', 'solar', 'wave');  -- generation type per site
CREATE TYPE turbine_model   AS ENUM ('L', 'M', 'H');  -- Low / Medium / High capacity
CREATE TYPE turbine_status  AS ENUM ('operational', 'maintenance', 'shutdown', 'failed', 'offline');

-- Operatives
CREATE TYPE skill_level AS ENUM ('junior', 'intermediate', 'senior', 'expert');
CREATE TYPE certification_type AS ENUM (
    'electrical', 'mechanical', 'hydraulic', 'blade_repair', 'offshore_safety',
    'high_voltage', 'rope_access', 'gwa_basic', 'gwa_advanced'
);
CREATE TYPE shift_type  AS ENUM ('day', 'night', 'on_call', 'off');

-- Work orders
CREATE TYPE work_order_type     AS ENUM ('preventive', 'corrective', 'inspection', 'emergency');
CREATE TYPE work_order_priority AS ENUM ('low', 'medium', 'high', 'critical');
CREATE TYPE work_order_status   AS ENUM ('scheduled', 'assigned', 'in_progress', 'on_hold', 'completed', 'cancelled');

-- Parts
CREATE TYPE part_category AS ENUM (
    'bearing', 'gearbox_component', 'generator_component', 'blade', 'hydraulic',
    'electrical', 'sensor', 'brake', 'yaw_system', 'pitch_system', 'cooling',
    'lubrication', 'fastener', 'seal', 'other'
);

-- ML predictions
CREATE TYPE prediction_type AS ENUM ('failure_binary', 'failure_probability', 'time_to_failure', 'failure_mode');

-- Alerts
CREATE TYPE alert_severity AS ENUM ('info', 'warning', 'critical', 'emergency');
CREATE TYPE alert_category AS ENUM (
    'ml_prediction', 'sensor_anomaly', 'weather_warning', 'maintenance_due',
    'parts_shortage', 'turbine_failure', 'operative_unavailable', 'system'
);
CREATE TYPE alert_status AS ENUM ('active', 'acknowledged', 'resolved', 'dismissed');

-- Agent actions
CREATE TYPE agent_action_status AS ENUM (
    'proposed', 'executed', 'rejected', 'cancelled', 'failed'
);
