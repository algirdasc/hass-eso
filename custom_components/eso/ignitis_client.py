import logging
import re
import time
import uuid
from collections import Counter
from datetime import date, datetime, timedelta

import requests

from .const import (
    AUTH_TYPE_SSO,
    DEFAULT_AUTH_TYPE,
    EXPORT_BALANCE_KEY,
    IGNITIS_RANGE_CHUNK_DAYS,
    POWER_CONSUMED,
    POWER_RETURNED,
)
from .eso_client import ESOAuthError, ESOConnectionError

LOGIN_URL = "https://energy-smart-api.ignitis.lt/api/users/login"
GENERATION_URL = "https://energy-smart-api.ignitis.lt/api/v2/objects/usage/{object}/day"

WEB_BASE = "https://e.ignitis.lt"
SSO_LOGIN_PAGE = f"{WEB_BASE}/sutartis/energySmart/prisijungti"
SSO_CALLBACK = "https://energy-smart-api.ignitis.lt/users/auth"
SSO_EXTERNAL_LOGIN = "https://energy-smart-api.ignitis.lt/api/users/external/login"
SSO_SESSION_COOKIE = ".AspNet.ApplicationCookie"
# The form action arrives as an unevaluated `{{...}}` template (client-rendered
# page), so it has to be reassembled around this id.
SSO_TEMP_USER_DATAS_RE = re.compile(r"tempUserDatasId=([0-9a-fA-F-]{36})")

_LOGGER = logging.getLogger(__name__)


class IgnitisClient:
    def __init__(
        self,
        username: str,
        password: str,
        auth_type: str = DEFAULT_AUTH_TYPE,
    ):
        self.username: str = username
        self.password: str = password
        self.auth_type: str = auth_type
        self.dataset: dict = {}
        self.session: requests.Session = requests.Session()
        self.token: str | None = None
        self._objects: list[dict] = []

    def login(self) -> None:
        self.dataset = {}
        if self.auth_type == AUTH_TYPE_SSO:
            login_response = self._login_sso()
        else:
            login_response = self._login_direct()
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

    def _post_direct_login(self) -> requests.Response:
        try:
            return self.session.post(
                LOGIN_URL,
                data={
                    "email": self.username,
                    "password": self.password,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
            )
        except requests.exceptions.RequestException as e:
            _LOGGER.error("Ignitis login error: %s", e)
            raise ESOConnectionError(str(e)) from e

    @staticmethod
    def _direct_credentials_rejected(response: requests.Response) -> bool:
        """Bad credentials come back as 400, not 401, with a per-field error
        object; a malformed request is a 400 whose `error` is a plain string."""
        if response.status_code in (401, 403):
            return True
        if response.status_code != 400:
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        return isinstance(body, dict) and isinstance(body.get("error"), dict)

    def _login_direct(self) -> dict:
        response = self._post_direct_login()
        if self._direct_credentials_rejected(response):
            raise ESOAuthError("Ignitis rejected the credentials")
        try:
            response.raise_for_status()
            _LOGGER.debug("Ignitis login response status: %s", response.status_code)
        except requests.exceptions.RequestException as e:
            _LOGGER.error("Ignitis login error: %s", e)
            raise ESOConnectionError(str(e)) from e
        try:
            return response.json()
        except ValueError as e:
            raise ESOConnectionError(f"Invalid Ignitis login response: {e}") from e

    def _login_sso(self) -> dict:
        """Mint a ticket, let the e.ignitis.lt web login bind it, trade it for an
        API token. One session throughout: the web cookie is SameSite=None and
        has to survive the cross-host hops.
        """
        ticket = str(uuid.uuid4()).upper()
        callback = f"{SSO_CALLBACK}?ticket={ticket}"
        self.session.cookies.clear()
        try:
            page = self.session.get(
                SSO_LOGIN_PAGE,
                params={"token": ticket, "callBackURL": callback},
            )
            page.raise_for_status()
        except requests.exceptions.RequestException as e:
            _LOGGER.error("Ignitis SSO login page error: %s", e)
            raise ESOConnectionError(str(e)) from e

        match = SSO_TEMP_USER_DATAS_RE.search(page.text)
        if not match:
            raise ESOConnectionError(
                "Ignitis SSO login page has no tempUserDatasId; the login page "
                "layout has probably changed"
            )
        # returnUrl must stay un-encoded; the endpoint rejects the encoded form.
        post_url = (
            f"{WEB_BASE}/prisijungti?returnUrl={callback}"
            "&newContract=true&contractRequestType="
            f"&tempUserDatasId={match.group(1)}"
            "&energySmart=true"
        )

        try:
            auth = self.session.post(
                post_url,
                data={"Email": self.username, "Password": self.password},
            )
            auth.raise_for_status()
        except requests.exceptions.RequestException as e:
            _LOGGER.error("Ignitis SSO credential post error: %s", e)
            raise ESOConnectionError(str(e)) from e

        # Bad credentials re-render the form with a 200 instead of redirecting,
        # so the cookie is the only success signal.
        if SSO_SESSION_COOKIE not in self.session.cookies:
            raise ESOAuthError("Ignitis rejected the credentials on the web login")
        if not any("/users/auth" in hop.url for hop in [*auth.history, auth]):
            raise ESOAuthError(
                "Ignitis SSO login never reached the ticket callback; the "
                "ticket is unbound"
            )

        try:
            response = self.session.post(
                SSO_EXTERNAL_LOGIN,
                params={"provider": "IGNITIS", "ticket": ticket},
            )
            if response.status_code in (401, 403):
                raise ESOAuthError("Ignitis refused to exchange the SSO ticket")
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            _LOGGER.error("Ignitis SSO ticket exchange error: %s", e)
            raise ESOConnectionError(str(e)) from e
        try:
            return response.json()
        except ValueError as e:
            raise ESOConnectionError(f"Invalid Ignitis login response: {e}") from e

    # ---- config-flow helpers ----------------------------------------------

    def check_password(self) -> bool:
        if self.auth_type == AUTH_TYPE_SSO:
            try:
                return bool(self._login_sso().get("token"))
            except ESOAuthError:
                return False
        response = self._post_direct_login()
        if self._direct_credentials_rejected(response):
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

    def fetch(
        self,
        obj: str,
        reference_date: datetime | None = None,
        date_range: tuple[date, date] | None = None,
    ) -> dict:
        headers = {
            "X-API-KEY": self.token,
        }
        if date_range is not None:
            first, last = date_range
        elif reference_date is not None:
            first = last = (reference_date - timedelta(days=1)).date()
        else:
            raise ValueError("fetch needs either reference_date or date_range")
        try:
            params = {
                "dateFrom": first.strftime("%Y-%m-%d"),
                "dateTo": last.strftime("%Y-%m-%d"),
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

    def fetch_dataset_range(
        self, obj: str, date_from: datetime, date_to: datetime
    ) -> dict:
        """Fetch hourly data for an arbitrary date range (history backfill).

        Ignitis publishes complete days only, so the range is clamped to
        yesterday. A single request serves at most 8 days and silently
        truncates a wider window to the most recent ones (no error, just
        missing days), hence the weekly chunks. The merged series is sorted
        chronologically because the statistics writer builds cumulative sums in
        iteration order.
        """
        self.dataset[obj] = {
            POWER_CONSUMED: {},
            POWER_RETURNED: {},
            EXPORT_BALANCE_KEY: None,
        }
        first = date_from.date()
        tzinfo = date_to.tzinfo or date_from.tzinfo
        last = min(date_to.date(), (datetime.now(tzinfo) - timedelta(days=1)).date())
        if first > last:
            _LOGGER.warning(
                "Ignitis: no published data for %s..%s yet (data is available "
                "up to %s)",
                first,
                date_to.date(),
                last,
            )
            return self.dataset[obj]
        chunks = []
        start = first
        while start <= last:
            end = min(start + timedelta(days=IGNITIS_RANGE_CHUNK_DAYS - 1), last)
            chunks.append((start, end))
            start = end + timedelta(days=1)
        gaps: list[str] = []
        for index, (start, end) in enumerate(chunks, start=1):
            if index > 1:
                time.sleep(1)
            _LOGGER.info(
                "Ignitis: fetching %s..%s (chunk %d of %d)",
                start,
                end,
                index,
                len(chunks),
            )
            # A failed request returns {} and is logged by `fetch`; truncation is
            # silent, so both have to be detected from the data itself.
            raw = self.fetch(obj, date_range=(start, end))
            gap = (
                self._describe_gap(self._merge_response(obj, raw), start, end)
                if raw
                else "request failed"
            )
            if gap:
                _LOGGER.warning(
                    "Ignitis: chunk %s..%s is incomplete (%s)", start, end, gap
                )
                gaps.append(f"{start}..{end}: {gap}")
        if gaps:
            # Backfills are not retried, so the log has to say what to re-run.
            _LOGGER.error(
                "Ignitis backfill for object %s is missing data — re-run "
                "import_now for the affected days: %s",
                obj,
                "; ".join(gaps),
            )
        for consumption_type in (POWER_CONSUMED, POWER_RETURNED):
            series: dict[float, float] = self.dataset[obj][consumption_type]
            self.dataset[obj][consumption_type] = dict(sorted(series.items()))
        return self.dataset[obj]

    @staticmethod
    def _describe_gap(parsed: dict, start: date, end: date) -> str | None:
        """Describe what a chunk is missing, or None if it looks complete.

        Counts hours rather than days: an 8-day window that comes back with a
        handful of hours per day is truncated just as much as one that drops the
        days entirely. 23 is the low bound because the March DST switch has a
        legitimately short day.
        """
        timestamps = set(parsed[POWER_CONSUMED]) | set(parsed[POWER_RETURNED])
        hours = Counter(datetime.fromtimestamp(ts).date() for ts in timestamps)
        expected = {
            start + timedelta(days=offset) for offset in range((end - start).days + 1)
        }
        parts = []
        if missing := sorted(str(day) for day in expected - set(hours)):
            parts.append(f"no data for {', '.join(missing)}")
        if short := sorted(
            f"{day} ({hours[day]}h)" for day in expected & set(hours) if hours[day] < 23
        ):
            parts.append(f"partial days {', '.join(short)}")
        return "; ".join(parts) or None

    def _merge_response(self, obj: str, data: dict) -> dict:
        parsed = self.parse_dataset(data)
        merged = self.dataset[obj]
        merged[POWER_CONSUMED].update(parsed[POWER_CONSUMED])
        merged[POWER_RETURNED].update(parsed[POWER_RETURNED])
        # Every chunk carries the same current balance; keep the last known one.
        if parsed[EXPORT_BALANCE_KEY] is not None:
            merged[EXPORT_BALANCE_KEY] = parsed[EXPORT_BALANCE_KEY]
        return parsed

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
