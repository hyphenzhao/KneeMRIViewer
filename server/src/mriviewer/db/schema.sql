PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS dataset(
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE NOT NULL,
  name TEXT,
  root_path TEXT NOT NULL,
  adapter TEXT NOT NULL,
  viewer TEXT DEFAULT 'mpr',
  delivery_profile TEXT DEFAULT 'native',
  default_label_set_id INTEGER REFERENCES label_set(id),
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS patient(
  id INTEGER PRIMARY KEY,
  dataset_id INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
  external_id TEXT NOT NULL,
  name TEXT, sex TEXT, age TEXT, birth_date TEXT,
  group_name TEXT,
  laterality TEXT,
  extra_json TEXT,
  UNIQUE(dataset_id, external_id)
);
CREATE INDEX IF NOT EXISTS ix_patient_ds ON patient(dataset_id);

CREATE TABLE IF NOT EXISTS study(
  id INTEGER PRIMARY KEY,
  patient_id INTEGER NOT NULL REFERENCES patient(id) ON DELETE CASCADE,
  study_uid TEXT, study_date TEXT, study_time TEXT,
  description TEXT, accession TEXT,
  path_rel TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_study_patient ON study(patient_id);

CREATE TABLE IF NOT EXISTS series(
  id INTEGER PRIMARY KEY,
  study_id INTEGER NOT NULL REFERENCES study(id) ON DELETE CASCADE,
  series_uid TEXT, series_number INTEGER,
  modality TEXT, description TEXT, path_rel TEXT NOT NULL,
  sop_class_uid TEXT, transfer_syntax_uid TEXT,
  manufacturer TEXT, model TEXT,
  rows INTEGER, cols INTEGER, n_slices INTEGER, n_instances INTEGER,
  bits_allocated INTEGER, pixel_representation INTEGER, dtype TEXT,
  pixel_spacing_json TEXT, slice_spacing REAL, slice_thickness REAL,
  origin_json TEXT, direction_json TEXT, frame_of_reference_uid TEXT,
  window_center REAL, window_width REAL,
  rescale_slope REAL DEFAULT 1, rescale_intercept REAL DEFAULT 0,
  acquisition_plane TEXT, anisotropy_ratio REAL,
  is_viewable INTEGER DEFAULT 1, geometry_warning TEXT,
  volume_state TEXT DEFAULT 'pending',
  volume_key TEXT, volume_bytes INTEGER, volume_error TEXT,
  indexed_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_series_study ON series(study_id);
CREATE INDEX IF NOT EXISTS ix_series_state ON series(volume_state);

CREATE TABLE IF NOT EXISTS instance(
  id INTEGER PRIMARY KEY,
  series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
  sop_instance_uid TEXT, instance_number INTEGER, slice_index INTEGER,
  path_rel TEXT NOT NULL, file_size INTEGER, mtime REAL, ipp_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_instance_series ON instance(series_id, slice_index);

CREATE TABLE IF NOT EXISTS label_set(
  id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL,
  name TEXT, name_en TEXT, source_path TEXT
);
CREATE TABLE IF NOT EXISTS label_def(
  id INTEGER PRIMARY KEY,
  label_set_id INTEGER NOT NULL REFERENCES label_set(id) ON DELETE CASCADE,
  value INTEGER NOT NULL, name TEXT NOT NULL, name_zh TEXT,
  color_hex TEXT NOT NULL, opacity REAL DEFAULT 0.7,
  structure_group TEXT, sort_order INTEGER,
  UNIQUE(label_set_id, value)
);

CREATE TABLE IF NOT EXISTS segmentation(
  id INTEGER PRIMARY KEY,
  series_id INTEGER REFERENCES series(id) ON DELETE CASCADE,
  dataset_id INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
  label_set_id INTEGER REFERENCES label_set(id),
  kind TEXT NOT NULL,
  source_path_rel TEXT NOT NULL,
  source_root TEXT,
  origin TEXT NOT NULL,
  display_name TEXT,
  model_name TEXT, model_version TEXT,
  parent_segmentation_id INTEGER REFERENCES segmentation(id),
  version_int INTEGER DEFAULT 1,
  dims_json TEXT, affine_json TEXT,
  ingest_transform_json TEXT,
  present_values_json TEXT,
  seg_state TEXT DEFAULT 'pending', seg_key TEXT, seg_error TEXT,
  mesh_state TEXT DEFAULT 'pending',
  stats_json TEXT,
  association_rule TEXT,
  is_active INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now')), created_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_seg_series ON segmentation(series_id, version_int);
-- One row per source file per dataset. Derived corrections get a new path
-- per version, so this stays unique across the whole version chain.
CREATE UNIQUE INDEX IF NOT EXISTS ux_seg_source ON segmentation(dataset_id, source_path_rel);

CREATE TABLE IF NOT EXISTS segmentation_mesh(
  id INTEGER PRIMARY KEY,
  segmentation_id INTEGER NOT NULL REFERENCES segmentation(id) ON DELETE CASCADE,
  label_value INTEGER NOT NULL,
  path_rel TEXT NOT NULL, n_points INTEGER, n_tris INTEGER, bytes INTEGER,
  params_json TEXT, built_at TEXT DEFAULT (datetime('now')),
  UNIQUE(segmentation_id, label_value)
);

CREATE TABLE IF NOT EXISTS edit_session(
  id TEXT PRIMARY KEY,
  base_segmentation_id INTEGER NOT NULL REFERENCES segmentation(id),
  series_id INTEGER NOT NULL REFERENCES series(id),
  created_by TEXT, created_at TEXT DEFAULT (datetime('now')),
  closed_at TEXT, saved_segmentation_id INTEGER REFERENCES segmentation(id),
  note TEXT
);

CREATE TABLE IF NOT EXISTS report(
  id INTEGER PRIMARY KEY,
  dataset_id INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
  external_id TEXT NOT NULL, age TEXT, sex TEXT, exam_time TEXT,
  department TEXT, body_part TEXT, findings TEXT, diagnosis TEXT,
  suggestion TEXT, raw_json TEXT,
  UNIQUE(dataset_id, external_id)
);
-- No FTS5 here on purpose. Its unicode61 tokenizer treats an unbroken run of
-- Chinese as a single token, so searching 软骨 against 「…股骨软骨变薄…」 returns
-- nothing while 积液 happens to match 178 reports purely by punctuation luck.
-- Getting CJK right would need a custom tokenizer (jieba et al.); at 2485 rows
-- (~1 MB of text) a LIKE scan is sub-millisecond and actually correct.
CREATE INDEX IF NOT EXISTS ix_report_ds ON report(dataset_id, external_id);

CREATE TABLE IF NOT EXISTS job(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
  dataset_id INTEGER, target_id INTEGER, priority INTEGER DEFAULT 100,
  status TEXT DEFAULT 'queued', progress REAL DEFAULT 0, message TEXT,
  payload_json TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  started_at TEXT, finished_at TEXT, attempts INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_job_queue ON job(status, priority, id);

CREATE TABLE IF NOT EXISTS scan_dir(
  id INTEGER PRIMARY KEY,
  dataset_id INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
  path_rel TEXT NOT NULL, dir_mtime REAL, entry_count INTEGER,
  total_size INTEGER, status TEXT, last_job_id INTEGER,
  UNIQUE(dataset_id, path_rel)
);
CREATE TABLE IF NOT EXISTS scan_error(
  id INTEGER PRIMARY KEY, job_id INTEGER, dataset_id INTEGER,
  path_rel TEXT, kind TEXT, message TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

-- Cartilage morphometry. The metric tree is stored as a JSON blob rather than
-- columns because its shape will keep changing as clinicians refine which
-- subregions and thresholds they want, and every such tweak would otherwise be
-- a migration.
--
-- The UNIQUE key includes algo_version and params_hash on purpose: changing a
-- threshold produces a NEW row instead of overwriting, so a number shown to a
-- doctor last week can still be reproduced and diffed against this week's.
CREATE TABLE IF NOT EXISTS segmentation_morphometry(
  id INTEGER PRIMARY KEY,
  segmentation_id INTEGER NOT NULL REFERENCES segmentation(id) ON DELETE CASCADE,
  algo_version TEXT NOT NULL,
  params_hash TEXT NOT NULL,
  params_json TEXT NOT NULL,
  frame_json TEXT,
  metrics_json TEXT,
  qc_json TEXT,
  state TEXT DEFAULT 'pending',
  error TEXT,
  duration_ms INTEGER,
  computed_at TEXT DEFAULT (datetime('now')),
  UNIQUE(segmentation_id, algo_version, params_hash)
);
CREATE INDEX IF NOT EXISTS ix_morph_seg
  ON segmentation_morphometry(segmentation_id, computed_at DESC);

-- AI-written report text. Note the table is `ai_report`, not `report`: that
-- name is already taken by the imported radiologist reports CSV.
--
-- Reports are never UPDATEd. Each generation inserts a new version_int so a
-- clinician can see what the system said last week alongside what it says now,
-- and so a signed-off version cannot be altered underneath the signature.
-- input_json stores the exact de-identified payload that was transmitted,
-- which is what makes the privacy claim auditable rather than merely asserted.
CREATE TABLE IF NOT EXISTS ai_report(
  id INTEGER PRIMARY KEY,
  segmentation_id INTEGER NOT NULL REFERENCES segmentation(id) ON DELETE CASCADE,
  morphometry_id INTEGER REFERENCES segmentation_morphometry(id),
  version_int INTEGER DEFAULT 1,
  lang TEXT DEFAULT 'zh',
  provider TEXT, model TEXT, base_url TEXT, prompt_hash TEXT,
  input_json TEXT,
  output_json TEXT,
  findings TEXT, quant TEXT, impression TEXT, advice TEXT,
  status TEXT,                -- ok | fallback | rejected_by_guardrail | failed
  error TEXT, guardrail_json TEXT,
  latency_ms INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER,
  review_state TEXT DEFAULT 'unreviewed',
  reviewed_by TEXT, reviewed_at TEXT, reviewed_text TEXT,
  created_at TEXT DEFAULT (datetime('now')), created_by TEXT
);
-- One report per segmentation. Regenerating replaces it rather than appending:
-- the clinician asked to see exactly one current report, not a history.
-- db/session.py collapses pre-existing duplicates before this index is created.
CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_report_seg ON ai_report(segmentation_id);

-- Runtime AI settings, editable from the web UI. A single row (id = 1).
--
-- The config file supplies defaults; a value here overrides it, so an operator
-- can point the platform at an in-house model without editing TOML and
-- restarting. NULL means "not set - fall back to the file".
--
-- The API key is deliberately NOT here: it is written to a 0600 file so it
-- cannot be read back out through the API or copied in a database backup.
CREATE TABLE IF NOT EXISTS ai_setting(
  id INTEGER PRIMARY KEY CHECK (id = 1),
  enabled INTEGER,
  allow_egress INTEGER,
  base_url TEXT,
  model TEXT,
  timeout_s REAL,
  updated_at TEXT DEFAULT (datetime('now')),
  updated_by TEXT
);
