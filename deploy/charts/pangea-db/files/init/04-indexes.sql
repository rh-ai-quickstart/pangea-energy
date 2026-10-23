-- ============================================================================
-- 04 - Indexes
-- ============================================================================

-- Sites
CREATE INDEX idx_sites_terrain ON sites(terrain);
CREATE INDEX idx_sites_active  ON sites(is_active);

-- Turbines
CREATE INDEX idx_turbines_site   ON turbines(site_id);
CREATE INDEX idx_turbines_status ON turbines(status);
CREATE INDEX idx_turbines_model  ON turbines(model);

-- Sensor readings (time-series access patterns)
CREATE INDEX idx_sensor_turbine ON sensor_readings(turbine_id, reading_time DESC);
CREATE INDEX idx_sensor_anomaly ON sensor_readings(reading_time DESC, anomaly_score DESC)
    WHERE anomaly_score > 0.7;

-- Operatives
CREATE INDEX idx_operatives_active         ON maintenance_operatives(is_active);
CREATE INDEX idx_operatives_skill          ON maintenance_operatives(skill_level);
CREATE INDEX idx_operatives_certifications ON maintenance_operatives USING GIN(certifications);

-- Operative schedules
CREATE INDEX idx_schedules_operative ON operative_schedules(operative_id, effective_date DESC);
CREATE INDEX idx_schedules_effective ON operative_schedules(effective_date, end_date);

-- Work orders
CREATE INDEX idx_work_orders_turbine   ON work_orders(turbine_id, created_at DESC);
CREATE INDEX idx_work_orders_status    ON work_orders(status, priority);
CREATE INDEX idx_work_orders_assigned  ON work_orders(assigned_operative_id, status);
CREATE INDEX idx_work_orders_scheduled ON work_orders(scheduled_start, scheduled_end);
CREATE INDEX idx_work_orders_type      ON work_orders(work_type, status);

-- Parts inventory
CREATE INDEX idx_parts_category ON parts_inventory(category);
CREATE INDEX idx_parts_critical ON parts_inventory(is_critical, quantity_on_hand);
CREATE INDEX idx_parts_reorder  ON parts_inventory(quantity_on_hand)
    WHERE quantity_on_hand <= reorder_point;

-- Parts requirements
CREATE INDEX idx_parts_req_work_order ON parts_requirements(work_order_id);
CREATE INDEX idx_parts_req_part       ON parts_requirements(part_id);

-- ML predictions
CREATE INDEX idx_predictions_turbine ON ml_predictions(turbine_id, prediction_time DESC);
CREATE INDEX idx_predictions_failure ON ml_predictions(prediction_time DESC)
    WHERE failure_predicted = TRUE;

-- Wind data
CREATE INDEX idx_wind_site ON wind_data(site_id, measurement_time DESC);

-- Alerts
CREATE INDEX idx_alerts_status   ON alerts(status, severity, alert_time DESC);
CREATE INDEX idx_alerts_turbine  ON alerts(turbine_id, alert_time DESC);
CREATE INDEX idx_alerts_site     ON alerts(site_id, alert_time DESC);
CREATE INDEX idx_alerts_category ON alerts(category, status);

-- Knowledge base
CREATE INDEX idx_knowledge_type ON knowledge_base(document_type, is_active);
CREATE INDEX idx_knowledge_tags ON knowledge_base USING GIN(tags);
-- Approximate nearest-neighbour index for embeddings (cosine distance).
CREATE INDEX idx_knowledge_embedding ON knowledge_base
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
