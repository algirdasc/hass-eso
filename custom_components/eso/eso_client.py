import logging
import re
import json
import time
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from .form_parser import FormParser
from .objects_parser import (
    SelectObjectsParser,
    clean_object_name,
)

LOGIN_URL = "https://mano.eso.lt/?destination=/consumption"
GENERATION_URL = "https://mano.eso.lt/consumption?ajax_form=1&_wrapper_format=drupal_ajax"
TFA_FORM_ID = "gpc_tfa_login_auth_form"
CONSUMPTION_FORM_ID = "eso_consumption_history_form"

class ESOError(Exception):
    """Base error for ESO client failures."""


class ESOConnectionError(ESOError):
    """Raised when the ESO service cannot be reached."""


class ESOAuthError(ESOError):
    """Raised when the supplied ESO credentials are rejected."""


class ESOTwoFactorError(ESOError):
    """Raised when a 2FA code is required but could not be obtained."""

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# How long to wait for the 2FA code email to arrive (the message is sent by
# ESO the moment the password POST lands on the TFA page).
OTP_POLL_TIMEOUT = 120
OTP_POLL_INTERVAL = 5
MONTHS = [
    "Sausio", "Vasario", "Kovo", "Balandžio", "Gegužės", "Birželio", "Liepos", "Rugpjūčio", "Rugsėjo", "Spalio", "Lapkričio", "Gruodžio"
]
_LOGGER = logging.getLogger(__name__)


class ESOClient:
    def __init__(self, username: str, password: str, imap_config: dict | None = None, session_file: str | None = None):
        self.username: str = username
        self.password: str = password
        self.imap_config: dict | None = imap_config
        self.session_file: str | None = session_file
        self.session: requests.Session = self._new_session()
        self.cookies: dict | None = None
        self.form_parser: FormParser = FormParser()
        self.dataset: dict = {}

    @staticmethod
    def _new_session() -> requests.Session:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        return session

    def login(self) -> None:
        """Establish an authenticated ESO session.

        Strategy: try to reuse a persisted Drupal session cookie, falling back
        to a full password -> email-OTP login when it is gone. NOTE: ESO runs
        Drupal's autologout module (see the Drupal.visitor.autologout_login
        cookie), which terminates idle sessions after a short inactivity
        window. A stored session was observed dead only a few hours later, so a
        once-daily run almost always faces a dead session and performs a full
        login. Reuse only helps for repeated fetches in quick succession. ESO
        emails a fresh
        code on *every* login, so the consumed OTP is deleted after use (see
        _delete_message) to keep it from accumulating in the inbox.
        """
        self.dataset = {}
        try:
            if self._load_session() and self._open_consumption():
                _LOGGER.info("ESO: reused stored session, skipping 2FA login")
                return
            _LOGGER.info("ESO: no valid stored session, performing full login")
            self.session = self._new_session()
            self._full_login()
            if self._open_consumption():
                self._save_session()
                _LOGGER.info("ESO: full login successful, session saved")
            else:
                _LOGGER.error("ESO login did not reach the consumption page")
        except requests.exceptions.RequestException as e:
            _LOGGER.error(f"ESO login error: {e}")
        except Exception as e:  # noqa: BLE001 - surface IMAP/parse failures too
            _LOGGER.error(f"ESO login failed: {e}")

    # ---- config-flow helpers ----------------------------------------------

    def check_password(self) -> bool:
        """Validate the ESO username/password without completing 2FA.

        Submits the login form on a throwaway session and reports whether the credentials were accepted:
        ESO either redirects to the TFA page (2FA enabled, credentials correct) or straight to the consumption page (no 2FA).
        A re-rendered login form means the credentials were rejected.

        Raises ESOConnectionError if the ESO service cannot be reached.
        """
        session = self._new_session()
        try:
            response = session.post(
                LOGIN_URL,
                data={
                    "name": self.username,
                    "pass": self.password,
                    "login_type": 1,
                    "form_id": "user_login_form",
                },
                allow_redirects=True,
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise ESOConnectionError(str(e)) from e
        if "/user/login/tfa/" in response.url:
            return True
        parser = FormParser()
        parser.feed(response.text)
        return parser.get("form_id") == CONSUMPTION_FORM_ID

    def discover_objects(self) -> list[dict]:
        """Return the account's objects as ``[{"id", "name"}]``.

        Performs a full login and scrapes the object IDs from the
        consumption-page selector. The display name is the option label with
        the trailing meter number stripped off.

        Raises:
            ESOConnectionError: ESO could not be reached.
            ESOAuthError: login did not reach the authenticated consumption page
                (wrong credentials or a 2FA step that could not be completed).
        """
        self.login()
        if self.form_parser.get("form_id") != CONSUMPTION_FORM_ID:
            raise ESOAuthError("Login did not reach the consumption page")
        try:
            response = self.session.get(LOGIN_URL, allow_redirects=True)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise ESOConnectionError(str(e)) from e
        select_parser = SelectObjectsParser()
        select_parser.feed(response.text)

        objects: list[dict] = []
        for obj_id, label in select_parser.objects.items():
            objects.append({"id": obj_id, "name": clean_object_name(label)})
        return objects

    def _open_consumption(self) -> bool:
        """GET the consumption page with the current session and parse its
        Drupal form tokens. Returns True when the session is authenticated
        (i.e. the consumption form is present rather than the login form)."""
        self.form_parser = FormParser()
        response = self.session.get(LOGIN_URL, allow_redirects=True)
        response.raise_for_status()
        self.cookies = requests.utils.dict_from_cookiejar(self.session.cookies)
        self.form_parser.feed(response.text)
        return self.form_parser.get("form_id") == CONSUMPTION_FORM_ID

    def _full_login(self) -> None:
        """Submit the username/password form. ESO redirects to the TFA page
        and emails a one-time code, which we then retrieve and submit."""
        response = self.session.post(
            LOGIN_URL,
            data={
                "name": self.username,
                "pass": self.password,
                "login_type": 1,
                "form_id": "user_login_form",
            },
            allow_redirects=True,
        )
        response.raise_for_status()
        if "/user/login/tfa/" not in response.url:
            # Either already logged in (unlikely with fresh session) or the
            # credentials were rejected. _open_consumption() will decide.
            _LOGGER.debug("ESO: no TFA redirect, login response url=%s", response.url)
            return
        login_started = datetime.now()
        tfa_url = response.url
        build_id = self._extract_tfa_build_id(response.text)
        if not build_id:
            _LOGGER.error("ESO: could not find TFA form_build_id on %s", tfa_url)
            return
        code = self._fetch_otp(login_started)
        if not code:
            _LOGGER.error("ESO: did not receive a 2FA code via IMAP in time")
            return
        _LOGGER.info("ESO: submitting 2FA code")
        submit = self.session.post(
            tfa_url,
            data={
                "code": code,
                "submit_code": "Submit code",
                "form_build_id": build_id,
                "form_id": TFA_FORM_ID,
            },
            allow_redirects=True,
        )
        submit.raise_for_status()

    @staticmethod
    def _extract_tfa_build_id(html: str) -> str | None:
        """Pull form_build_id out of the gpc-tfa-login-auth-form only, so we
        don't pick up another form's token from the same page."""
        marker = "gpc-tfa-login-auth-form"
        if marker not in html:
            return None
        segment = html.split(marker, 1)[1].split("</form>", 1)[0]
        for tag in re.findall(r"<input[^>]+>", segment):
            if "form_build_id" in tag:
                value = re.search(r'value="([^"]*)"', tag)
                if value:
                    return value.group(1)
        return None

    # ---- IMAP one-time-code retrieval -------------------------------------

    def _fetch_otp(self, login_started: datetime) -> str | None:
        if not self.imap_config:
            _LOGGER.error("ESO: 2FA required but no IMAP config provided")
            return None
        cfg = self.imap_config
        deadline = time.monotonic() + OTP_POLL_TIMEOUT
        # Only accept messages that arrived after we triggered the login,
        # with a small clock-skew allowance, so we never reuse a stale code.
        min_time = login_started - timedelta(minutes=2)
        while time.monotonic() < deadline:
            try:
                code = self._poll_imap_once(cfg, min_time)
                if code:
                    return code
            except Exception as e:  # noqa: BLE001
                _LOGGER.warning("ESO: IMAP poll error: %s", e)
            time.sleep(OTP_POLL_INTERVAL)
        return None

    def _poll_imap_once(self, cfg: dict, min_time: datetime) -> str | None:
        conn = imaplib.IMAP4_SSL(cfg["host"], cfg.get("port", 993))
        try:
            conn.login(cfg["username"], cfg["password"])
            conn.select(cfg.get("folder", "INBOX"))
            since = min_time.strftime("%d-%b-%Y")
            typ, data = conn.search(None, f'(FROM "{cfg["sender"]}" SINCE {since})')
            if typ != "OK" or not data or not data[0]:
                return None
            ids = data[0].split()
            # Newest first.
            for msg_id in reversed(ids):
                typ, msg_data = conn.fetch(msg_id, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                msg_dt = self._parse_msg_date(msg)
                if msg_dt and msg_dt < min_time:
                    continue
                code = self._extract_code(self._message_text(msg))
                if code:
                    # The code is single-use; remove the email so it doesn't
                    # pile up in the inbox (ESO sends one on every login, and
                    # its autologout forces a fresh login on each daily run).
                    self._delete_message(conn, msg_id)
                    return code
            return None
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _delete_message(conn, msg_id) -> None:
        """Delete the consumed OTP email and expunge it from the folder.

        Standard IMAP delete (\\Deleted + EXPUNGE). On Gmail this removes the
        message from the searched folder (e.g. it leaves the inbox); other
        servers remove it outright. Best-effort: a failure here must never
        block a successful login."""
        try:
            conn.store(msg_id, "+FLAGS", "\\Deleted")
            conn.expunge()
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("ESO: could not delete consumed OTP email: %s", e)

    @staticmethod
    def _parse_msg_date(msg) -> datetime | None:
        raw = msg.get("Date")
        if not raw:
            return None
        try:
            dt = email.utils.parsedate_to_datetime(raw)
            # Compare naively in local time; strip tz to match login_started.
            return dt.astimezone().replace(tzinfo=None)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _message_text(msg) -> str:
        parts = []
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype in ("text/plain", "text/html"):
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        parts.append(payload.decode(charset, errors="replace"))
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                parts.append(payload.decode(charset, errors="replace"))
        return "\n".join(parts)

    @staticmethod
    def _extract_code(text: str) -> str | None:
        if not text:
            return None
        # Strip HTML tags so "Jūsų kodas:" and the code aren't split by markup.
        plain = re.sub(r"<[^>]+>", " ", text)
        plain = re.sub(r"\s+", " ", plain)
        # Anchor on the "kodas" label, then take the next 6-digit group.
        m = re.search(r"kodas\D{0,40}?(\d{6})", plain, re.IGNORECASE)
        if m:
            return m.group(1)
        # Fallback: any standalone 6-digit number.
        m = re.search(r"(?<!\d)(\d{6})(?!\d)", plain)
        return m.group(1) if m else None

    # ---- session persistence ----------------------------------------------

    def _save_session(self) -> None:
        if not self.session_file:
            return
        try:
            jar = requests.utils.dict_from_cookiejar(self.session.cookies)
            with open(self.session_file, "w") as fh:
                json.dump(jar, fh)
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("ESO: could not save session: %s", e)

    def _load_session(self) -> bool:
        if not self.session_file:
            return False
        try:
            with open(self.session_file) as fh:
                jar = json.load(fh)
        except FileNotFoundError:
            return False
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("ESO: could not load session: %s", e)
            return False
        if not jar:
            return False
        self.session = self._new_session()
        self.session.cookies = requests.utils.cookiejar_from_dict(jar)
        return True

    def fetch(
        self,
        obj: str,
        date: datetime,
        date_range: tuple[datetime, datetime] | None = None,
    ) -> dict:
        if not self.cookies:
            _LOGGER.error("Cookies are empty. Check your credentials.")
            return {}
        if self.form_parser.get("form_id") != CONSUMPTION_FORM_ID:
            _LOGGER.error("Form ID not found. Check your credentials OR login to ESO and confirm contact information.")
            return {}
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
        if date_range is not None:
            # The weekly view ignores active_date_value and always renders the
            # last 7 days; the "Kita" (custom) period is the only server-side
            # path to historical hourly data.
            data["period"] = "other"
            data["other_start"] = date_range[0].strftime("%Y-%m-%d")
            data["other_end"] = date_range[1].strftime("%Y-%m-%d")
        try:
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
        except requests.exceptions.RequestException as e:
            _LOGGER.error(f"ESO fetch error: {e}")
            return {}

    def fetch_dataset(self, obj: str, date: datetime) -> dict | None:
        if obj in self.dataset:
            return self.dataset[obj]
        self.dataset[obj] = {}
        self._merge_response(obj, self.fetch(obj, date))
        return self.dataset[obj]

    def fetch_dataset_range(
        self, obj: str, date_from: datetime, date_to: datetime
    ) -> dict | None:
        """Fetch hourly data for an arbitrary date range (history backfill).

        Uses the form's "Kita" (custom) period, which returns the whole range
        hourly in one response; very long ranges are split into ~90-day
        requests. The merged series is sorted chronologically because the
        statistics writer builds cumulative sums in iteration order."""
        self.dataset[obj] = {}
        start = date_from
        while start <= date_to:
            end = min(start + timedelta(days=89), date_to)
            self._merge_response(obj, self.fetch(obj, end, date_range=(start, end)))
            start = end + timedelta(days=1)
            if start <= date_to:
                time.sleep(1)
        for consumption_type, series in self.dataset[obj].items():
            self.dataset[obj][consumption_type] = dict(sorted(series.items()))
        return self.dataset[obj]

    def _merge_response(self, obj: str, data: dict) -> None:
        for d in data:
            if d.get("command") == "update_build_id":
                self.form_parser.set("form_build_id", d["new"])
                continue
            if d.get("command") != "settings":
                continue
            if "eso_consumption_history_form" not in d["settings"] or not d["settings"]["eso_consumption_history_form"]:
                continue
            datasets = d["settings"]["eso_consumption_history_form"]["graphics_data"]["datasets"]
            for dataset in datasets:
                consumption_type = dataset["key"]
                if consumption_type not in self.dataset[obj]:
                    self.dataset[obj][consumption_type] = {}
                self.dataset[obj][consumption_type].update(self.parse_dataset(dataset))

    def get_dataset(self, obj: str) -> dict | None:
        if obj not in self.dataset:
            return None
        return self.dataset[obj]

    @staticmethod
    def parse_dataset(dataset: dict) -> dict:
        result = {}
        for record in dataset["record"]:
            try:
                dt = datetime.strptime(record["date"], "%Y%m%d%H%M")
                ts = dt.timestamp()
                val = abs(float(record["value"])) if record["value"] is not None else 0.0
                result[ts] = val
            except Exception as e:
                _LOGGER.error(f"Failed to parse dataset record {record}: {e}")
        return result
