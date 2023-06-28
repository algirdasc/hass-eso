import logging
from datetime import datetime

import requests

from .form_parser import FormParser

LOGIN_URL = "https://mano.eso.lt/?destination=/consumption"
#GENERATION_URL = "https://mano.eso.lt/consumption?ajax_form=1&_wrapper_format=drupal_ajax"
GENERATION_URL = "http://test.esprimo.lan/test/"

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
        self.consumption: dict = {}

    def login(self) -> None:
        self.consumption = {}

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

        if len(response.cookies) == 0:
            _LOGGER.error("Failed to get cookies after login. Possible invalid credentials")
            return

        self.cookies = requests.utils.dict_from_cookiejar(response.cookies)

        self.form_parser.feed(response.text)

    def fetch(self, obj: str, date: datetime) -> dict:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        }

        response = self.session.post(
            GENERATION_URL,
            data={
                "objects": [obj],
                "display_type": "hourly",
                "period": "week",
                "day_period": date.strftime("%Y-%m-%d"),
                "energy_type": "general",
                "scales": "total",
                "visible_scales_field": 0,
                "visible_last_year_comparison_field": 0,
                "form_build_id": self.form_parser.get("form_build_id"),
                "form_token": self.form_parser.get("form_token"),
                "form_id": self.form_parser.get("form_id"),
                "_drupal_ajax": "1",
                "_triggering_element_name": "period",
            },
            headers=headers,
            cookies=self.cookies,
            allow_redirects=False
        )

        response.raise_for_status()

        _LOGGER.debug(f"Got generation response: {response.text}")

        return response.json()

    def fetch_consumption_data(self, obj: str, date: datetime) -> None:
        if obj in self.consumption:
            return

        self.consumption[obj] = {}

        data = self.fetch(obj, date)

        for d in data:
            if d["command"] != "settings":
                continue

            if "eso_consumption_history_form" not in d["settings"] or not d["settings"]["eso_consumption_history_form"]:
                continue

            datasets = d["settings"]["eso_consumption_history_form"]["graphics_data"]["datasets"]

            for dataset in datasets:
                consumption_type = dataset["key"]
                if consumption_type not in self.consumption[obj]:
                    self.consumption[obj][consumption_type] = {}
                self.consumption[obj][consumption_type] = self.parse_dataset(dataset)

    def get_consumption_data(self, obj: str) -> dict | None:
        if obj not in self.consumption:
            return None

        return self.consumption[obj]

    @staticmethod
    def parse_dataset(dataset: dict) -> dict:
        result = {}

        for record in dataset["record"]:
            ts = int(datetime.timestamp(datetime.strptime(record["date"], "%Y%m%d%H%M%S")))
            result[ts] = float(record["value"]) if record["value"] is not None else 0.0

        return result
