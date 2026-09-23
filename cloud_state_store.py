"""CloudBase MySQL persistence for the Smart Kitchen UI state.

The CloudBase API key stays in the Cloud Run environment. Android and web
clients access only this application's authenticated state endpoint.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


class CloudStateStore:
    """Persist the singleton kitchen state using CloudBase's MySQL HTTP API."""

    TABLE = "smart_kitchen_state"
    RECORD_ID = "global"

    def __init__(self, logger):
        self.logger = logger
        self.env_id = os.environ.get("CLOUDBASE_ENV_ID", "").strip()
        self.api_key = (
            os.environ.get("CLOUDBASE_APIKEY", "").strip()
            or os.environ.get("CLOUDBASE_API_KEY", "").strip()
        )
        self.enabled = bool(self.env_id and self.api_key)
        self._record_exists = False
        self.base_url = (
            f"https://{self.env_id}.api.tcloudbasegateway.com/v1/rdb/rest/{self.TABLE}"
            if self.enabled else ""
        )
        if self.enabled:
            self.logger.info("CloudBase state storage is enabled.")
        else:
            self.logger.info("CloudBase state storage is not configured; using local state only.")

    def _request(self, method, url, payload=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else None

    def save(self, state):
        if not self.enabled:
            return
        payload = {
            "id": self.RECORD_ID,
            "state_json": json.dumps(state, ensure_ascii=False, separators=(",", ":")),
            "updated_at": int(time.time()),
        }
        try:
            if self._record_exists:
                filters = urllib.parse.urlencode({"id": f"eq.{self.RECORD_ID}"})
                self._request("PATCH", f"{self.base_url}?{filters}", payload)
            else:
                self._request("POST", self.base_url, payload)
                self._record_exists = True
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                try:
                    filters = urllib.parse.urlencode({"id": f"eq.{self.RECORD_ID}"})
                    self._request("PATCH", f"{self.base_url}?{filters}", payload)
                    self._record_exists = True
                    return
                except Exception as retry_exc:
                    self.logger.error("Could not persist CloudBase state: %s", retry_exc)
                    return
            self.logger.error("Could not persist CloudBase state: HTTP %s", exc.code)
        except Exception as exc:
            self.logger.error("Could not persist CloudBase state: %s", exc)

    def load(self):
        if not self.enabled:
            return None
        try:
            params = urllib.parse.urlencode({"select": "state_json", "id": f"eq.{self.RECORD_ID}", "limit": "1"})
            _, rows = self._request("GET", f"{self.base_url}?{params}")
            if isinstance(rows, list) and rows:
                self._record_exists = True
                value = json.loads(rows[0].get("state_json", "{}"))
                return value if isinstance(value, dict) else None
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                self.logger.error("Could not load CloudBase state: HTTP %s", exc.code)
        except Exception as exc:
            self.logger.error("Could not load CloudBase state: %s", exc)
        return None
