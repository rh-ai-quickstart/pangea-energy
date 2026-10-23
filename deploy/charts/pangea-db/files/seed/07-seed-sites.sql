-- ============================================================================
-- 07 - Seed: sites, parts, operatives, schedules, knowledge base
-- Reference/operational data. Turbines and time-series are seeded in 08/09.
-- ============================================================================
SELECT setseed(0.42);

-- ---------------------------------------------------------------------------
-- Wind sites (5 of 6: 15+25+20+18+12 = 90 turbines; BJF adds 14 below)
-- ---------------------------------------------------------------------------
INSERT INTO sites (site_name, site_code, terrain, latitude, longitude, elevation_meters,
                   turbine_count, commissioned_date, avg_wind_speed_ms, min_temp_celsius,
                   max_temp_celsius, access_difficulty, nearest_city, country, travel_time_hours) VALUES
('Calanda',  'MTP', 'mountain',  46.802700,   9.836100, 1850, 15, DATE '2017-06-15', 8.4, -25,  22, 'difficult',         'Chur',        'Switzerland', 3.5),
('Blavand',  'NSO', 'offshore',  54.900000,   6.500000,    0, 25, DATE '2019-03-20', 11.2,  -5,  24, 'weather-dependent', 'Esbjerg',     'Denmark', 4.0),
('Odra',     'MWP', 'plains',    53.428500,  14.552900,   45, 20, DATE '2016-09-01', 7.8,  -20,  33, 'easy',              'Szczecin',    'Poland', 1.0),
('Sintra',   'CRG', 'coastal',   38.544900,  -9.006500,  120, 18, DATE '2018-11-10', 9.1,   2,  33, 'moderate',          'Lisbon',      'Portugal', 1.5),
('Tabernas', 'DFL', 'desert',    37.003600,  -2.400900,  520, 12, DATE '2020-05-05', 6.9,   2,  44, 'easy',              'Almería',     'Spain', 0.8);

-- Northern Norway (Arctic) wind farm at Björnefjäll, near Narvik. Exposed
-- mountain terrain, high mean wind, harsh winters. Turbines are generated from
-- turbine_count in seed 08 (time-series in 09) like the other wind sites.
INSERT INTO sites (site_name, site_code, terrain, latitude, longitude, elevation_meters,
                   turbine_count, commissioned_date, avg_wind_speed_ms, min_temp_celsius,
                   max_temp_celsius, access_difficulty, nearest_city, country, travel_time_hours) VALUES
('Björnefjäll Wind Farm', 'BJF', 'mountain', 68.430000, 18.130000, 520, 14, DATE '2024-08-15', 9.6, -30, 20, 'weather-dependent', 'Narvik', 'Norway', 1.0);

-- ---------------------------------------------------------------------------
-- Solar plants (3, in sunny Southern Europe). No turbines: generation is
-- estimated in real time by the OpenShift AI solar-output model from live
-- weather (see backend /api/solar/predict). capacity_kw = rated DC capacity.
-- ---------------------------------------------------------------------------
INSERT INTO sites (site_name, site_code, terrain, energy_type, latitude, longitude, elevation_meters,
                   turbine_count, capacity_kw, commissioned_date, min_temp_celsius, max_temp_celsius,
                   access_difficulty, nearest_city, country, travel_time_hours) VALUES
('Guadalquivir Solar', 'GDQ', 'plains',  'solar', 37.389100,  -5.984500,   12, 0, 60000, DATE '2022-04-12',  1, 44, 'easy', 'Seville',   'Spain', 0.6),
('Alentejo Solar',     'ALT', 'plains',  'solar', 38.571400,  -7.913500,  245, 0, 50000, DATE '2021-09-30',  0, 41, 'easy', 'Évora',     'Portugal', 0.9),
('Trinacria Solar',    'TRN', 'coastal', 'solar', 37.075500,  15.286600,   40, 0, 45000, DATE '2023-03-08',  4, 40, 'easy', 'Syracuse',  'Italy', 0.7);

-- ---------------------------------------------------------------------------
-- Wave farms (2, North Atlantic / Norwegian Sea). No turbines: output is
-- estimated in real time by the backend (/api/wave/predict) from live sea
-- state (Open-Meteo Marine wave height + period -> wave energy flux). Farm
-- design (100-buoy array, ~7.4 MW rated) is derived from the UCI Wave Energy
-- Converters dataset; capacity_kw = rated array output.
-- ---------------------------------------------------------------------------
INSERT INTO sites (site_name, site_code, terrain, energy_type, latitude, longitude, elevation_meters,
                   turbine_count, capacity_kw, commissioned_date, min_temp_celsius, max_temp_celsius,
                   access_difficulty, nearest_city, country, travel_time_hours) VALUES
('Orkney Wave Farm', 'ORK', 'coastal', 'wave', 59.000000,  -3.600000, 0, 0, 7400, DATE '2023-06-15', -2, 18, 'difficult', 'Kirkwall', 'United Kingdom', 1.5),
('Runde Wave Farm',  'RUN', 'coastal', 'wave', 62.500000,   5.000000, 0, 0, 7360, DATE '2024-05-20', -5, 20, 'difficult', 'Ålesund',  'Norway', 2.0);

-- ---------------------------------------------------------------------------
-- Parts inventory (mix of common wear items + critical long-lead parts;
-- a few are intentionally below reorder point to populate parts_to_reorder)
-- ---------------------------------------------------------------------------
INSERT INTO parts_inventory (part_number, part_name, category, compatible_models, manufacturer,
                             quantity_on_hand, quantity_reserved, reorder_point, reorder_quantity,
                             unit_cost, lead_time_days, warehouse_location, is_critical) VALUES
('BRG-MAIN-001', 'Main shaft bearing',        'bearing',             ARRAY['M','H']::turbine_model[], 'SKF',        3, 1, 2,  2,  18500.00, 90, 'WH-A / R1', TRUE),
('GBX-SEAL-014', 'Gearbox oil seal kit',      'gearbox_component',   ARRAY['L','M','H']::turbine_model[], 'Bosch',  22, 5, 8, 20,    240.00, 14, 'WH-A / R3', FALSE),
('GEN-BRSH-007', 'Generator brush set',       'generator_component', ARRAY['L','M']::turbine_model[], 'ABB',       15, 2, 6, 15,    180.00, 10, 'WH-A / R4', FALSE),
('PIT-MTR-022',  'Blade pitch motor',         'pitch_system',        ARRAY['M','H']::turbine_model[], 'Moog',       4, 1, 2,  4,   6200.00, 45, 'WH-B / R1', TRUE),
('YAW-MTR-019',  'Yaw drive motor',           'yaw_system',          ARRAY['L','M','H']::turbine_model[], 'Bonfiglioli', 6, 0, 3, 6,  3100.00, 30, 'WH-B / R2', FALSE),
('SNS-VIB-003',  'Vibration sensor (triax)',  'sensor',              ARRAY['L','M','H']::turbine_model[], 'PCB',    40, 6,12, 30,    320.00,  7, 'WH-A / R6', FALSE),
('SNS-TMP-004',  'Temperature sensor PT100',  'sensor',              ARRAY['L','M','H']::turbine_model[], 'IFM',    55, 3,15, 40,     45.00,  5, 'WH-A / R6', FALSE),
('HYD-FLD-030',  'Hydraulic fluid (20L)',     'hydraulic',           ARRAY['L','M','H']::turbine_model[], 'Shell',  28, 0,10, 30,     95.00,  7, 'WH-C / R1', FALSE),
('LUB-OIL-031',  'Gearbox lubrication oil',   'lubrication',         ARRAY['L','M','H']::turbine_model[], 'Mobil',  12, 2, 8, 24,    140.00,  7, 'WH-C / R2', FALSE),
('BRK-PAD-041',  'Rotor brake pad set',       'brake',               ARRAY['M','H']::turbine_model[], 'Svendborg', 9, 1, 4, 10,    520.00, 21, 'WH-B / R4', FALSE),
('BLD-REP-050',  'Blade repair composite kit','blade',               ARRAY['L','M','H']::turbine_model[], 'Gurit',   5, 0, 3,  6,   1450.00, 28, 'WH-B / R6', TRUE),
('ELC-FUSE-060', 'HV fuse assembly',          'electrical',          ARRAY['L','M','H']::turbine_model[], 'Eaton',  18, 0, 8, 20,     75.00, 10, 'WH-A / R7', FALSE),
('COOL-FAN-070', 'Nacelle cooling fan',       'cooling',             ARRAY['M','H']::turbine_model[], 'ebm-papst', 2, 0, 3,  6,    410.00, 18, 'WH-B / R3', FALSE),
('SEAL-NAC-080', 'Nacelle weather seal',      'seal',                ARRAY['L','M','H']::turbine_model[], 'Trelleborg', 7, 1, 4, 10,  210.00, 12, 'WH-C / R4', FALSE),
('FST-BLT-090',  'Tower flange bolt set',     'fastener',            ARRAY['L','M','H']::turbine_model[], 'Nord-Lock', 60, 1,20, 50,   38.00,  9, 'WH-C / R5', FALSE);

-- ---------------------------------------------------------------------------
-- Maintenance operatives (15: 3 junior, 6 intermediate, 4 senior, 2 expert)
-- ---------------------------------------------------------------------------
INSERT INTO maintenance_operatives (employee_code, first_name, last_name, email, skill_level,
                                    certifications, specializations, base_location,
                                    max_travel_distance_km, offshore_certified, rope_access_certified,
                                    high_altitude_certified, hire_date, hourly_rate, on_leave_until) VALUES
('OP-001','Astrid','Nygaard','astrid.nygaard@pangea.example','expert',
   ARRAY['electrical','mechanical','offshore_safety','high_voltage','gwa_basic','gwa_advanced']::certification_type[],
   ARRAY['gearbox','generator'], 'Esbjerg', 2000, TRUE, TRUE, TRUE, DATE '2015-02-01', 68.00, NULL),
('OP-002','Samuel','Okafor','samuel.okafor@pangea.example','expert',
   ARRAY['electrical','mechanical','blade_repair','offshore_safety','high_voltage','rope_access','gwa_basic','gwa_advanced']::certification_type[],
   ARRAY['blade_inspection','offshore'], 'Lisbon', 2000, TRUE, TRUE, TRUE, DATE '2015-08-15', 70.00, NULL),
('OP-003','Katarzyna','Wójcik','katarzyna.wojcik@pangea.example','senior',
   ARRAY['electrical','mechanical','high_voltage','gwa_basic','gwa_advanced']::certification_type[],
   ARRAY['generator','electrical_systems'], 'Szczecin', 1200, FALSE, FALSE, TRUE, DATE '2016-05-10', 55.00, NULL),
('OP-004','Lukas','Haugen','lukas.haugen@pangea.example','senior',
   ARRAY['mechanical','hydraulic','rope_access','gwa_basic','gwa_advanced']::certification_type[],
   ARRAY['pitch_system','hydraulics'], 'Chur', 1000, FALSE, TRUE, TRUE, DATE '2016-11-22', 56.00, NULL),
('OP-005','Freja','Sørensen','freja.sorensen@pangea.example','senior',
   ARRAY['electrical','mechanical','blade_repair','offshore_safety','gwa_basic','gwa_advanced']::certification_type[],
   ARRAY['blade_inspection'], 'Esbjerg', 1500, TRUE, TRUE, TRUE, DATE '2017-03-30', 54.00, NULL),
('OP-006','Elena','Navarro','elena.navarro@pangea.example','senior',
   ARRAY['electrical','mechanical','high_voltage','gwa_basic','gwa_advanced']::certification_type[],
   ARRAY['gearbox'], 'Almería', 900, FALSE, FALSE, TRUE, DATE '2017-07-18', 53.00, CURRENT_DATE + 1),
('OP-007','Mariana','Ferreira','mariana.ferreira@pangea.example','intermediate',
   ARRAY['electrical','mechanical','gwa_basic']::certification_type[],
   ARRAY['electrical_systems'], 'Lisbon', 700, FALSE, FALSE, TRUE, DATE '2019-01-14', 42.00, NULL),
('OP-008','Piotr','Kowalski','piotr.kowalski@pangea.example','intermediate',
   ARRAY['mechanical','hydraulic','gwa_basic']::certification_type[],
   ARRAY['hydraulics'], 'Szczecin', 700, FALSE, FALSE, TRUE, DATE '2019-04-02', 41.00, NULL),
('OP-009','Ingrid','Andersen','ingrid.andersen@pangea.example','intermediate',
   ARRAY['electrical','mechanical','offshore_safety','gwa_basic']::certification_type[],
   ARRAY['generator'], 'Esbjerg', 900, TRUE, FALSE, TRUE, DATE '2019-09-11', 43.00, NULL),
('OP-010','Nadia','Rossi','nadia.rossi@pangea.example','intermediate',
   ARRAY['electrical','mechanical','blade_repair','gwa_basic']::certification_type[],
   ARRAY['blade_inspection'], 'Chur', 600, FALSE, TRUE, TRUE, DATE '2020-02-20', 40.00, NULL),
('OP-011','Javier','Moreno','javier.moreno@pangea.example','intermediate',
   ARRAY['electrical','mechanical','high_voltage','gwa_basic']::certification_type[],
   ARRAY['electrical_systems'], 'Almería', 700, FALSE, FALSE, TRUE, DATE '2020-06-08', 42.00, NULL),
('OP-012','Tomás','Silva','tomas.silva@pangea.example','intermediate',
   ARRAY['mechanical','hydraulic','gwa_basic']::certification_type[],
   ARRAY['pitch_system'], 'Lisbon', 600, FALSE, FALSE, TRUE, DATE '2021-03-15', 39.00, NULL),
('OP-013','Robin','Haas','robin.haas@pangea.example','junior',
   ARRAY['electrical','mechanical','gwa_basic']::certification_type[],
   ARRAY['general'], 'Szczecin', 400, FALSE, FALSE, FALSE, DATE '2023-05-02', 30.00, CURRENT_DATE - 3),
('OP-014','Lucía','Ortega','lucia.ortega@pangea.example','junior',
   ARRAY['mechanical','gwa_basic']::certification_type[],
   ARRAY['general'], 'Almería', 400, FALSE, FALSE, FALSE, DATE '2023-09-18', 29.00, NULL),
('OP-015','Frankie','Sato','frankie.sato@pangea.example','junior',
   ARRAY['electrical','mechanical','gwa_basic']::certification_type[],
   ARRAY['general'], 'Chur', 400, FALSE, FALSE, TRUE, DATE '2024-01-08', 30.00, NULL);

-- ---------------------------------------------------------------------------
-- Operative schedules. Exception dates are relative to the install date so the
-- demo calendar stays populated however long after seeding it is opened.
-- ---------------------------------------------------------------------------
INSERT INTO operative_schedules (operative_id, effective_date, monday, tuesday, wednesday,
                                 thursday, friday, saturday, sunday,
                                 exception_dates, exception_reason)
SELECT o.operative_id, CURRENT_DATE - 180, v.mon::shift_type, v.tue::shift_type, v.wed::shift_type,
       v.thu::shift_type, v.fri::shift_type, v.sat::shift_type, v.sun::shift_type,
       v.exceptions, v.reason
FROM (VALUES
    ('OP-001','day','day','day','day','on_call','off','off',
        ARRAY[CURRENT_DATE + 3, CURRENT_DATE + 4], 'GWO advanced refresher, Esbjerg'),
    ('OP-002','off','day','day','day','day','day','off',
        NULL::date[], NULL),
    ('OP-003','day','day','day','day','day','off','off',
        ARRAY[CURRENT_DATE + 7], 'High-voltage recertification'),
    ('OP-004','day','day','day','on_call','on_call','off','off',
        NULL::date[], NULL),
    ('OP-005','off','off','day','day','day','day','day',
        NULL::date[], NULL),
    ('OP-006','day','day','day','day','day','on_call','off',
        NULL::date[], NULL),
    ('OP-007','day','day','day','day','day','off','off',
        NULL::date[], NULL),
    ('OP-008','night','night','night','night','off','off','off',
        NULL::date[], NULL),
    ('OP-009','day','day','day','off','off','day','day',
        NULL::date[], NULL),
    ('OP-010','off','day','day','day','day','day','off',
        ARRAY[CURRENT_DATE], 'Rope access reassessment'),
    ('OP-011','day','day','day','day','day','off','on_call',
        NULL::date[], NULL),
    ('OP-012','off','off','night','night','night','night','night',
        NULL::date[], NULL),
    ('OP-013','day','day','day','day','day','off','off',
        ARRAY[CURRENT_DATE + 5], 'Mentored climb assessment'),
    ('OP-014','day','day','day','off','off','day','day',
        NULL::date[], NULL),
    ('OP-015','day','day','day','day','off','off','off',
        ARRAY[CURRENT_DATE + 7, CURRENT_DATE + 8], 'GWO basic renewal, Chur')
) AS v(code, mon, tue, wed, thu, fri, sat, sun, exceptions, reason)
JOIN maintenance_operatives o ON o.employee_code = v.code;

-- Fortnight overrides; supersede the base rota until end_date.
INSERT INTO operative_schedules (operative_id, effective_date, end_date, monday, tuesday, wednesday,
                                 thursday, friday, saturday, sunday, exception_reason)
SELECT o.operative_id, CURRENT_DATE - 7, CURRENT_DATE + 6,
       v.mon::shift_type, v.tue::shift_type, v.wed::shift_type, v.thu::shift_type,
       v.fri::shift_type, v.sat::shift_type, v.sun::shift_type, v.reason
FROM (VALUES
    ('OP-002','night','night','night','night','off','off','off',
        'Temporary night cover for the North Sea shutdown backlog'),
    ('OP-009','off','off','day','day','day','day','day',
        'Swapped onto the North Sea offshore rotation')
) AS v(code, mon, tue, wed, thu, fri, sat, sun, reason)
JOIN maintenance_operatives o ON o.employee_code = v.code;

-- ---------------------------------------------------------------------------
-- Knowledge base. Embeddings are NULL; nothing retrieves these yet.
-- ---------------------------------------------------------------------------
INSERT INTO knowledge_base (document_name, document_type, title, content, summary, tags, turbine_models) VALUES
('gearbox-oil-change.md','procedure','Gearbox oil change procedure',
 'Standard procedure for gearbox lubrication oil replacement. Shut down and lock out the turbine. Confirm rotor is braked. Drain used oil into approved container. Replace filter (LUB-OIL-031). Refill to specified level. Record oil analysis sample.',
 'How to safely change gearbox oil.', ARRAY['gearbox','lubrication','maintenance'], ARRAY['L','M','H']::turbine_model[]),
('vibration-troubleshooting.md','troubleshooting','Main shaft vibration troubleshooting',
 'Rising main-shaft vibration typically indicates bearing wear or misalignment. Compare triaxial vibration trend against baseline. If X-axis RMS exceeds 4.5 mm/s and trending up, schedule bearing inspection (BRG-MAIN-001). Check for temperature correlation.',
 'Diagnosing main shaft vibration increases.', ARRAY['vibration','bearing','diagnostics'], ARRAY['M','H']::turbine_model[]),
('offshore-safety.md','safety','Offshore access safety protocol',
 'Offshore turbine access requires GWO offshore safety certification and weather within limits (wind < 15 m/s, wave height < 1.5 m). Always work in pairs. Confirm vessel standby. Use fall-arrest at all times.',
 'Safety rules for offshore turbine access.', ARRAY['safety','offshore','access'], ARRAY['M','H']::turbine_model[]);
