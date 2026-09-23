"""Cloud MySQL persistence for the Smart Kitchen UI state.

The application remains usable locally without database environment variables.
When DB_HOST/DB_USER/DB_PASSWORD/DB_NAME are configured, every state revision is
stored in MySQL so stateless CloudBase instances and Android clients share state.
"""

import json
import os


class CloudStateStore:
    def __init__(self, logger):
        self.logger = logger
        self.host = os.environ.get("DB_HOST", "").strip()
        self.user = os.environ.get("DB_USER", "").strip()
        self.password = os.environ.get("DB_PASSWORD", "")
        self.database = os.environ.get("DB_NAME", "").strip()
        self.port = int(os.environ.get("DB_PORT", "3306"))
        self.enabled = bool(self.host and self.user and self.password and self.database)
        self._driver = None
        if self.enabled:
            try:
                import pymysql
                self._driver = pymysql
                self._ensure_table()
                self.logger.info("Cloud state storage is enabled.")
            except Exception as exc:  # Service must still start when DB is temporarily unavailable.
                self.enabled = False
                self.logger.error("Cloud state storage disabled: %s", exc)

    def _connect(self):
        return self._driver.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            charset="utf8mb4",
            connect_timeout=5,
            read_timeout=5,
            write_timeout=5,
            autocommit=True,
        )

    def _ensure_table(self):
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS smart_kitchen_state (
                        state_id TINYINT UNSIGNED NOT NULL PRIMARY KEY,
                        state_json LONGTEXT NOT NULL,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )

    def save(self, state):
        if not self.enabled:
            return
        try:
            payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO smart_kitchen_state (state_id, state_json)
                        VALUES (1, %s)
                        ON DUPLICATE KEY UPDATE state_json=VALUES(state_json)
                        """,
                        (payload,),
                    )
        except Exception as exc:
            self.logger.error("Could not persist cloud state: %s", exc)

    def load(self):
        if not self.enabled:
            return None
        try:
            with self._connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT state_json FROM smart_kitchen_state WHERE state_id=1")
                    row = cursor.fetchone()
            if row and row[0]:
                value = json.loads(row[0])
                return value if isinstance(value, dict) else None
        except Exception as exc:
            self.logger.error("Could not load cloud state: %s", exc)
        return None
