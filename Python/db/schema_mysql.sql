-- =====================================================================
-- MySQL schema for the Intelligent Document Service.
--
-- This mirrors the SQLAlchemy models and the Alembic migration. It exists so a
-- DBA can provision (or review) the schema without running Python.
--
--   mysql -u root -p < db/schema_mysql.sql
--
-- For an application deployment prefer:  alembic upgrade head
-- That records the revision in `alembic_version`, so later migrations apply
-- cleanly. If you run this script instead, stamp the baseline afterwards:
--   alembic stamp head
-- =====================================================================

CREATE DATABASE IF NOT EXISTS appdb
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE appdb;

-- ---------------------------------------------------------------- users
CREATE TABLE IF NOT EXISTS users (
    id              INT          NOT NULL AUTO_INCREMENT,
    email           VARCHAR(255) NOT NULL,
    full_name       VARCHAR(255) NULL,
    hashed_password VARCHAR(255) NOT NULL,
    -- Stored as the lowercase VALUE ('user'/'admin'), matching the JSON API and
    -- the ORM. A native ENUM is avoided so adding a role needs no ALTER TYPE.
    role            VARCHAR(20)  NOT NULL DEFAULT 'user',
    is_active       TINYINT(1)   NOT NULL DEFAULT 1,
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY ix_users_email (email),
    KEY ix_users_role (role)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- ------------------------------------------------------- refresh_tokens
-- One row per issued refresh token. Only the `jti` is stored, never the token
-- string, so a database leak does not hand over usable sessions.
CREATE TABLE IF NOT EXISTS refresh_tokens (
    id             INT          NOT NULL AUTO_INCREMENT,
    jti            VARCHAR(64)  NOT NULL,
    user_id        INT          NOT NULL,
    expires_at     DATETIME     NOT NULL,
    revoked_at     DATETIME     NULL,
    -- 'rotated' | 'logout' | 'password_change' | 'security' | 'admin'.
    -- Load-bearing: replaying a *rotated* token signals theft and revokes every
    -- session; replaying a *logged-out* one is a stale client and fails alone.
    revoked_reason VARCHAR(20)  NULL,
    user_agent     VARCHAR(255) NULL,
    ip_address     VARCHAR(45)  NULL,          -- 45 chars fits IPv6
    created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY ix_refresh_tokens_jti (jti),
    KEY ix_refresh_tokens_user_id (user_id),
    -- Serves "revoke every live session for this user" without a table scan.
    KEY ix_refresh_tokens_user_revoked (user_id, revoked_at),
    CONSTRAINT fk_refresh_tokens_user FOREIGN KEY (user_id)
        REFERENCES users (id) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- ------------------------------------------------------------ documents
-- Small, hot, heavily queried: metadata and lifecycle only. The bulky OCR text
-- lives in document_extractions so listing 50 documents stays cheap.
CREATE TABLE IF NOT EXISTS documents (
    id                     INT          NOT NULL AUTO_INCREMENT,
    owner_id               INT          NOT NULL,

    filename               VARCHAR(255) NOT NULL,
    content_type           VARCHAR(127) NOT NULL,
    size_bytes             INT          NOT NULL,
    checksum_sha256        VARCHAR(64)  NOT NULL,

    storage_key            VARCHAR(512) NOT NULL,   -- opaque; resolved by the backend
    storage_backend        VARCHAR(20)  NOT NULL,   -- 'local' | 's3'

    -- 'pending' | 'processing' | 'completed' | 'failed'
    status                 VARCHAR(20)  NOT NULL DEFAULT 'pending',
    attempt_count          INT          NOT NULL DEFAULT 0,
    processing_started_at  DATETIME     NULL,
    processing_finished_at DATETIME     NULL,
    error_code             VARCHAR(64)  NULL,
    error_message          VARCHAR(1024) NULL,

    -- Denormalised from the extraction so the list view can filter without a join.
    document_type          VARCHAR(64)  NULL,
    page_count             INT          NULL,

    created_at             DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at             DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    -- Per-user deduplication: the same file uploaded twice is not re-analysed.
    UNIQUE KEY uq_documents_owner_checksum (owner_id, checksum_sha256),
    KEY ix_documents_owner_id (owner_id),
    KEY ix_documents_status (status),
    KEY ix_documents_document_type (document_type),
    KEY ix_documents_checksum_sha256 (checksum_sha256),
    -- Backs the default listing: this owner's documents, filtered by status,
    -- newest first.
    KEY ix_documents_owner_status_created (owner_id, status, created_at),
    CONSTRAINT fk_documents_owner FOREIGN KEY (owner_id)
        REFERENCES users (id) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- ------------------------------------------------- document_extractions
-- The AI output. One row per document, replaced in place on reprocess.
CREATE TABLE IF NOT EXISTS document_extractions (
    id                   INT         NOT NULL AUTO_INCREMENT,
    document_id          INT         NOT NULL,

    -- LONGTEXT, not TEXT: TEXT caps at 64 KB, which a 40-page scan exceeds.
    raw_text             LONGTEXT    NOT NULL,
    text_char_count      INT         NOT NULL DEFAULT 0,
    page_count           INT         NULL,
    ocr_provider         VARCHAR(32) NOT NULL,      -- 'local' | 'textract'
    ocr_duration_ms      INT         NULL,

    analysis_provider    VARCHAR(32) NOT NULL,      -- 'openai' | 'heuristic'
    analysis_model       VARCHAR(64) NULL,
    analysis_duration_ms INT         NULL,
    prompt_tokens        INT         NULL,          -- per-document cost attribution
    completion_tokens    INT         NULL,

    document_type        VARCHAR(64) NULL,
    language             VARCHAR(16) NULL,
    summary              TEXT        NULL,
    confidence           FLOAT       NULL,

    -- JSON because the shape varies by document type (an invoice has line items,
    -- a contract has parties) and these values are never queried into.
    keywords             JSON        NOT NULL,
    entities             JSON        NOT NULL,
    fields               JSON        NOT NULL,
    warnings             JSON        NOT NULL,

    created_at           DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at           DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP
                                     ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY ix_document_extractions_document_id (document_id),
    CONSTRAINT fk_extractions_document FOREIGN KEY (document_id)
        REFERENCES documents (id) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- ------------------------------------------------------ document_events
-- Append-only audit trail. Answers "why did this fail at 2am, and which
-- provider was to blame".
CREATE TABLE IF NOT EXISTS document_events (
    id          INT          NOT NULL AUTO_INCREMENT,
    document_id INT          NOT NULL,
    event       VARCHAR(48)  NOT NULL,
    message     VARCHAR(512) NULL,
    payload     JSON         NOT NULL,
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY ix_document_events_document_id (document_id),
    KEY ix_document_events_document_created (document_id, created_at),
    CONSTRAINT fk_document_events_document FOREIGN KEY (document_id)
        REFERENCES documents (id) ON DELETE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_unicode_ci;

-- ----------------------------------------------------- application user
-- Prefer a least-privilege account over root. Change the password first.
-- CREATE USER IF NOT EXISTS 'appuser'@'%' IDENTIFIED BY 'change_me_strong_password';
-- GRANT SELECT, INSERT, UPDATE, DELETE ON appdb.* TO 'appuser'@'%';
-- -- Alembic additionally needs DDL rights:
-- -- GRANT CREATE, ALTER, DROP, INDEX, REFERENCES ON appdb.* TO 'appuser'@'%';
-- FLUSH PRIVILEGES;
