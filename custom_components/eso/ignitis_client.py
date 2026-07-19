import logging
from datetime import datetime, timedelta

import requests

from .const import EXPORT_BALANCE_KEY, POWER_CONSUMED, POWER_RETURNED
from .eso_client import ESOAuthError, ESOConnectionError

LOGIN_URL = "https://energy-smart-api.ignitis.lt/api/users/login"
GENERATION_URL = "https://energy-smart-api.ignitis.lt/api/v2/objects/usage/{object}/day"
_LOGGER = logging.getLogger(__name__)


class IgnitisClient:
    def __init__(
        self,
        username: str,
        password: str,
        imap_config: dict | None = None,
        session_file: str | None = None,
    ):
        self.username: str = username
        self.password: str = password
        self.dataset: dict = {}
        self.session: requests.Session = requests.Session()
        self.token: str | None = None
        self._objects: list[dict] = []

    def login(self) -> None:
        self.dataset = {}
        try:
            headers = {
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            }
            response = self.session.post(
                LOGIN_URL,
                data={
                    "email": self.username,
                    "password": self.password,
                },
                headers=headers,
            )
            response.raise_for_status()
            _LOGGER.debug("Ignitis login response status: %s", response.status_code)
        except requests.exceptions.RequestException as e:
            _LOGGER.error("Ignitis login error: %s", e)
            raise ESOConnectionError(str(e)) from e
        try:
            login_response = response.json()
        except ValueError as e:
            raise ESOConnectionError(f"Invalid Ignitis login response: {e}") from e
        token = login_response.get("token")
        if not token:
            raise ESOAuthError("Ignitis login did not return a token")
        self.token = token
        self._objects = []
        for obj in login_response.get("user", {}).get("objects", []):
            uoid = obj.get("uoid")
            if uoid is None:
                continue
            _LOGGER.info("Found object: %s, address: %s", uoid, obj.get("address"))
            self._objects.append(
                {"id": str(uoid), "name": obj.get("address") or str(uoid)}
            )

    # ---- config-flow helpers ----------------------------------------------

    def check_password(self) -> bool:
        try:
            response = self.session.post(
                LOGIN_URL,
                data={"email": self.username, "password": self.password},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
                },
            )
        except requests.exceptions.RequestException as e:
            raise ESOConnectionError(str(e)) from e
        if response.status_code in (401, 403):
            return False
        try:
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise ESOConnectionError(str(e)) from e
        try:
            return bool(response.json().get("token"))
        except ValueError as e:
            raise ESOConnectionError(f"Invalid Ignitis response: {e}") from e

    def discover_objects(self) -> list[dict]:
        self.login()
        if not self.token:
            raise ESOAuthError("Ignitis login did not return a token")
        return list(self._objects)

    def fetch(self, obj: str, date: datetime) -> dict:
        headers = {
            "X-API-KEY": self.token,
        }
        yesterday = date - timedelta(days=1)
        try:
            params = {
                "dateFrom": yesterday.strftime("%Y-%m-%d"),
                "dateTo": yesterday.strftime("%Y-%m-%d"),
                "interval": "hour",
            }
            response = self.session.get(
                GENERATION_URL.replace("{object}", obj),
                params=params,
                headers=headers,
            )
            if response.status_code in (401, 403):
                raise ESOAuthError("Ignitis rejected the API token")
            response.raise_for_status()
            _LOGGER.debug("Got fetch response: %s", response.text)
            return response.json()
        except requests.exceptions.RequestException as e:
            _LOGGER.error("Ignitis fetch error: %s", e)
            return {}

    def fetch_dataset(self, obj: str, date: datetime) -> dict | None:
        self.dataset[obj] = {}
        data = self.fetch(obj, date)
        self.dataset[obj] = self.parse_dataset(data)
        return self.dataset[obj]

    def get_dataset(self, obj: str) -> dict | None:
        if obj not in self.dataset:
            return None
        return self.dataset[obj]

    @staticmethod
    def parse_dataset(dataset: dict) -> dict:
        result: dict = {POWER_CONSUMED: {}, POWER_RETURNED: {}, EXPORT_BALANCE_KEY: None}
        export = dataset.get("exportBalance")
        if isinstance(export, dict) and export.get("balance") is not None:
            result[EXPORT_BALANCE_KEY] = export["balance"]
        for record in dataset.get("data", []):
            try:
                timestamp = datetime.strptime(
                    record["startTime"], "%Y-%m-%d %H:%M:%S"
                ).timestamp()
                result[POWER_CONSUMED][timestamp] = record.get("consumed") or 0.0
                result[POWER_RETURNED][timestamp] = record.get("supplied") or 0.0
            except Exception as e:  # noqa: BLE001
                _LOGGER.error("Failed to parse dataset record %s: %s", record, e)
        return result
