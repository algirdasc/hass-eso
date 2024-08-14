import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from .form_parser import FormParser

LOGIN_URL = "https://mano.eso.lt/?destination=/consumption"
GENERATION_URL = "https://mano.eso.lt/consumption?ajax_form=1&_wrapper_format=drupal_ajax"

MONTHS = [
    "Sausio", "Vasario", "Kovo",
    "Balandžio", "Gegužės", "Birželio",
    "Liepos", "Rugpjūčio", "Rugsėjo",
    "Spalio", "Lapkričio", "Gruodžio"
]

_LOGGER = logging.getLogger(__name__)


class ESOClient:

    def __init__(self, username: str, password: str):
        self.username: str = username
        self.password: str = password
        self.session: requests.Session = requests.Session()
        self.cookies: dict | None = None
        self.form_parser: FormParser = FormParser()
        self.dataset: dict = {}

    def login(self) -> None:
        self.dataset = {}

        response = self.session.post(
            LOGIN_URL,
            data={
                "name": self.username,
                "pass": self.password,
                "login_type": 1,
                "form_id": "user_login_form"
            },
            allow_redirects=True
        )

        response.raise_for_status()

        _LOGGER.debug(f"Got login response: {response.text}")

        self.cookies = requests.utils.dict_from_cookiejar(response.cookies)

        self.form_parser.feed(response.text)

    def fetch(self, obj: str, date: datetime) -> dict:
        if not self.cookies:
            raise Exception("Cookies are empty. Check your credentials.")

        if self.form_parser.get("form_id") != "eso_consumption_history_form":
            raise Exception("Form ID not found. Check your credentials OR login to ESO and confirm contact information.")

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        }

        data = {
                "objects[]": obj,
                "objects_mock": "",
                "display_type": "hourly",
                "period": "week",
                "energy_type": "general",
                "scales": "total",
                "active_date_value": date.strftime("%Y-%m-%d 00:00"),
                "made_energy_status": 1,
                "visible_scales_field": 0,
                "visible_last_year_comparison_field": 0,
                "form_build_id": self.form_parser.get("form_build_id"),
                "form_token": self.form_parser.get("form_token"),
                "form_id": self.form_parser.get("form_id"),
                "_drupal_ajax": "1",
                "_triggering_element_name": "display_type",
            }

        response = self.session.post(
            GENERATION_URL,
            data=data,
            headers=headers,
            cookies=self.cookies,
            allow_redirects=False
        )

        response.raise_for_status()

        _LOGGER.debug(f"Got fetch response: {response.text}")

        return response.json()

    def fetch_dataset(self, obj: str, date: datetime) -> None:
        if obj in self.dataset:
            return

        self.dataset[obj] = {}

        data = self.fetch(obj, date)

        for d in data:
            if d["command"] == "update_build_id":
                self.form_parser.set("form_build_id", d["new"])
                continue

            if d["command"] != "settings":
                continue

            if "eso_consumption_history_form" not in d["settings"] or not d["settings"]["eso_consumption_history_form"]:
                continue

            datasets = d["settings"]["eso_consumption_history_form"]["graphics_data"]["datasets"]

            for dataset in datasets:
                consumption_type = dataset["key"]
                if consumption_type not in self.dataset[obj]:
                    self.dataset[obj][consumption_type] = {}
                self.dataset[obj][consumption_type] = self.parse_dataset(dataset)

    def get_dataset(self, obj: str) -> dict | None:
        if obj not in self.dataset:
            return None

        return self.dataset[obj]

    @staticmethod
    def parse_dataset(dataset: dict) -> dict:
        zoneinfo_vln = ZoneInfo("Europe/Vilnius")
        result = {}

        for record in dataset["record"]:
            dt = datetime.strptime(record["date"], "%Y%m%d%H%M")
            dt = dt.replace(tzinfo=zoneinfo_vln)
            # ESO date indicates hourly period end, HA needs period start
            dt = dt - timedelta(hours=1)
            ts = dt.timestamp()
            result[ts] = abs(float(record["value"])) if record["value"] is not None else 0.0

        return result
