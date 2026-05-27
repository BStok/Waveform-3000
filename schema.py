import psycopg2
from psycopg2.extras import execute_values
from psycopg2.extras import execute_batch

SCHEMA_SQL = """

-- USERS TABLE (for auth)
CREATE TABLE IF NOT EXISTS users (
    
    user_id UUID PRIMARY KEY,
    username TEXT UNIQUE ,
    email TEXT UNIQUE,
    password_hash TEXT ,
    google_id TEXT UNIQUE,
    auth_method TEXT DEFAULT 'password',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

    -- added for google auth on 27/5
    email_verified BOOLEAN DEFAULT FALSE,
    otp_hash TEXT,
    otp_expiry TIMESTAMP,
    reset_otp_hash TEXT,
    reset_otp_expiry TIMESTAMP;
);

CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_google_id ON users(google_id);


-- SONGS TABLE (immutable record)
CREATE TABLE IF NOT EXISTS songs (
    song_id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    duration_seconds INT DEFAULT 0,
    storage_path TEXT,
    status TEXT NOT NULL DEFAULT 'downloading',
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT unique_user_source UNIQUE (user_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_songs_user_id ON songs(user_id);
CREATE INDEX IF NOT EXISTS idx_songs_status ON songs(status);


-- PLAYLISTS TABLE
CREATE TABLE IF NOT EXISTS playlists (
    playlist_id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_playlists_user_id ON playlists(user_id);


-- PLAYLIST SONGS (maintains order)
CREATE TABLE IF NOT EXISTS playlist_songs (
    playlist_id UUID NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
    song_id UUID NOT NULL REFERENCES songs(song_id) ON DELETE CASCADE,
    position INT NOT NULL,
    PRIMARY KEY (playlist_id, song_id)
);


-- DOWNLOAD JOBS (async state tracking)
CREATE TABLE IF NOT EXISTS download_jobs (
    job_id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued',
    total_songs INT NOT NULL,
    downloaded_count INT DEFAULT 0,
    failed_count INT DEFAULT 0,
    current_song TEXT DEFAULT '',
    cancelled BOOLEAN DEFAULT FALSE,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON download_jobs(user_id);


-- JOB RESULTS (per-song tracking)
CREATE TABLE IF NOT EXISTS job_results (
    job_id UUID NOT NULL REFERENCES download_jobs(job_id) ON DELETE CASCADE,
    song_id UUID,
    song_query TEXT NOT NULL,
    status TEXT NOT NULL,
    error_message TEXT,
    PRIMARY KEY (job_id, song_query)
);

"""
def init_schema(conn):
    cur = conn.cursor()
    cur.execute(SCHEMA_SQL)
    conn.commit()
    cur.close()
    print("✓ Schema initialized")

if __name__ == "__main__":
    conn = psycopg2.connect(
        dbname="music_library",
        user="postgres",
        password="your_password",
        host="localhost",
        port=5432
    )
    init_schema(conn)
    conn.close()