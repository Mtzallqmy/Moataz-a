-- Generated from app.db.Base. Initial schema for a dedicated Neon database.

CREATE TABLE IF NOT EXISTS users (
	id SERIAL NOT NULL, 
	telegram_id BIGINT NOT NULL, 
	username VARCHAR(255), 
	language VARCHAR(5) NOT NULL, 
	is_allowed BOOLEAN NOT NULL, 
	is_admin BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_users_telegram_id ON users (telegram_id);

CREATE TABLE IF NOT EXISTS worker_nodes (
	id VARCHAR(128) NOT NULL, 
	hostname VARCHAR(255) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	active_jobs INTEGER NOT NULL, 
	last_seen TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS download_jobs (
	id SERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	chat_id BIGINT NOT NULL, 
	progress_message_id BIGINT, 
	source_url TEXT NOT NULL, 
	platform VARCHAR(32) NOT NULL, 
	title TEXT, 
	duration INTEGER, 
	thumbnail TEXT, 
	selected_quality VARCHAR(32), 
	cut_start FLOAT, 
	cut_end FLOAT, 
	worker_id VARCHAR(128), 
	status VARCHAR(32) NOT NULL, 
	progress FLOAT NOT NULL, 
	speed VARCHAR(64), 
	eta VARCHAR(64), 
	output_path TEXT, 
	file_size BIGINT, 
	error TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX IF NOT EXISTS ix_download_jobs_status ON download_jobs (status);

CREATE INDEX IF NOT EXISTS ix_download_jobs_user_id ON download_jobs (user_id);

CREATE INDEX IF NOT EXISTS ix_download_jobs_worker_id ON download_jobs (worker_id);

CREATE TABLE IF NOT EXISTS media_assets (
	id SERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	asset_type VARCHAR(32) NOT NULL, 
	source_type VARCHAR(32) NOT NULL, 
	source_url TEXT, 
	telegram_file_id TEXT, 
	local_path TEXT NOT NULL, 
	mime_type VARCHAR(255), 
	duration FLOAT, 
	width INTEGER, 
	height INTEGER, 
	file_size BIGINT NOT NULL, 
	metadata_json TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_media_assets_file_size_positive CHECK (file_size > 0), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX IF NOT EXISTS ix_media_assets_asset_type ON media_assets (asset_type);

CREATE INDEX IF NOT EXISTS ix_media_assets_source_type ON media_assets (source_type);

CREATE INDEX IF NOT EXISTS ix_media_assets_user_created ON media_assets (user_id, created_at);

CREATE INDEX IF NOT EXISTS ix_media_assets_user_id ON media_assets (user_id);

CREATE TABLE IF NOT EXISTS media_projects (
	id SERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	chat_id BIGINT NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	aspect_ratio VARCHAR(16) NOT NULL, 
	width INTEGER NOT NULL, 
	height INTEGER NOT NULL, 
	fps INTEGER NOT NULL, 
	timeline_json TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX IF NOT EXISTS ix_media_projects_status ON media_projects (status);

CREATE INDEX IF NOT EXISTS ix_media_projects_user_id ON media_projects (user_id);

CREATE INDEX IF NOT EXISTS ix_media_projects_user_updated ON media_projects (user_id, updated_at);

CREATE TABLE IF NOT EXISTS job_events (
	id SERIAL NOT NULL, 
	job_id INTEGER NOT NULL, 
	event_type VARCHAR(64) NOT NULL, 
	message TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(job_id) REFERENCES download_jobs (id)
);

CREATE INDEX IF NOT EXISTS ix_job_events_event_type ON job_events (event_type);

CREATE INDEX IF NOT EXISTS ix_job_events_job_id ON job_events (job_id);

CREATE TABLE IF NOT EXISTS media_asset_analyses (
	asset_id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	analyzer_version VARCHAR(32) NOT NULL, 
	source_fingerprint VARCHAR(128) NOT NULL, 
	status VARCHAR(24) NOT NULL, 
	analysis_json TEXT NOT NULL, 
	error TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (asset_id), 
	FOREIGN KEY(asset_id) REFERENCES media_assets (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX IF NOT EXISTS ix_media_asset_analyses_status ON media_asset_analyses (status);

CREATE INDEX IF NOT EXISTS ix_media_asset_analyses_user_id ON media_asset_analyses (user_id);

CREATE INDEX IF NOT EXISTS ix_media_asset_analysis_status_updated ON media_asset_analyses (status, updated_at);

CREATE TABLE IF NOT EXISTS media_metadata (
	job_id INTEGER NOT NULL, 
	uploader TEXT, 
	formats_json TEXT NOT NULL, 
	normalized_url TEXT NOT NULL, 
	is_playlist BOOLEAN NOT NULL, 
	playlist_count INTEGER NOT NULL, 
	cut_mode VARCHAR(16) NOT NULL, 
	source VARCHAR(16) NOT NULL, 
	priority INTEGER NOT NULL, 
	PRIMARY KEY (job_id), 
	FOREIGN KEY(job_id) REFERENCES download_jobs (id)
);

CREATE INDEX IF NOT EXISTS ix_media_metadata_normalized_url ON media_metadata (normalized_url);

CREATE TABLE IF NOT EXISTS project_assets (
	project_id INTEGER NOT NULL, 
	asset_id INTEGER NOT NULL, 
	position INTEGER NOT NULL, 
	role VARCHAR(32) NOT NULL, 
	PRIMARY KEY (project_id, asset_id), 
	CONSTRAINT uq_project_assets_project_position UNIQUE (project_id, position), 
	CONSTRAINT ck_project_assets_position_nonnegative CHECK (position >= 0), 
	FOREIGN KEY(project_id) REFERENCES media_projects (id) ON DELETE CASCADE, 
	FOREIGN KEY(asset_id) REFERENCES media_assets (id)
);

CREATE INDEX IF NOT EXISTS ix_project_assets_position ON project_assets (position);

CREATE TABLE IF NOT EXISTS render_jobs (
	id SERIAL NOT NULL, 
	project_id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	progress FLOAT NOT NULL, 
	output_path TEXT, 
	file_size BIGINT, 
	error TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_render_jobs_progress_range CHECK (progress >= 0 AND progress <= 1), 
	FOREIGN KEY(project_id) REFERENCES media_projects (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX IF NOT EXISTS ix_render_jobs_project_created ON render_jobs (project_id, created_at);

CREATE INDEX IF NOT EXISTS ix_render_jobs_project_id ON render_jobs (project_id);

CREATE INDEX IF NOT EXISTS ix_render_jobs_status ON render_jobs (status);

CREATE INDEX IF NOT EXISTS ix_render_jobs_user_id ON render_jobs (user_id);

CREATE INDEX IF NOT EXISTS ix_render_jobs_user_status ON render_jobs (user_id, status);

CREATE TABLE IF NOT EXISTS studio_agent_messages (
	id SERIAL NOT NULL, 
	project_id INTEGER NOT NULL, 
	role VARCHAR(16) NOT NULL, 
	content TEXT NOT NULL, 
	provider_id VARCHAR(64), 
	model VARCHAR(255), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(project_id) REFERENCES media_projects (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_studio_agent_messages_project_id ON studio_agent_messages (project_id);

CREATE INDEX IF NOT EXISTS ix_studio_agent_project_created ON studio_agent_messages (project_id, created_at);

CREATE TABLE IF NOT EXISTS timeline_revisions (
	id SERIAL NOT NULL, 
	project_id INTEGER NOT NULL, 
	revision INTEGER NOT NULL, 
	timeline_json TEXT NOT NULL, 
	operation_json TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_timeline_revision_project_number UNIQUE (project_id, revision), 
	FOREIGN KEY(project_id) REFERENCES media_projects (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_timeline_revision_project_created ON timeline_revisions (project_id, created_at);

CREATE INDEX IF NOT EXISTS ix_timeline_revisions_project_id ON timeline_revisions (project_id);

CREATE TABLE IF NOT EXISTS render_requests (
	render_job_id INTEGER NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	max_duration FLOAT, 
	max_width INTEGER, 
	PRIMARY KEY (render_job_id), 
	FOREIGN KEY(render_job_id) REFERENCES render_jobs (id) ON DELETE CASCADE
);
