CREATE TABLE IF NOT EXISTS mcp_pat (
  id           BIGINT PRIMARY KEY,
  user_id      BIGINT NOT NULL,
  project_id   BIGINT NULL,
  name         VARCHAR(64) NOT NULL,
  token_hash   CHAR(64) NOT NULL,
  prefix       VARCHAR(16) NOT NULL,
  create_time  DATETIME NOT NULL,
  expires_at   DATETIME NULL,
  last_used_at DATETIME NULL,
  revoked_at   DATETIME NULL,
  UNIQUE KEY uk_mcp_pat_hash (token_hash),
  KEY idx_mcp_pat_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
