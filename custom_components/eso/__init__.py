import logging
from datetime import timedelta, datetime
import asyncio
import random
import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticMetaData,
    StatisticData,
    StatisticMeanType,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics, statistics_during_period,
)
from homeassistant.const import (
    CONF_ID, CONF_NAME, CONF_USERNAME, CONF_PASSWORD, UnitOfEnergy
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util
from .eso_client import ESOClient

_LOGGER = logging.getLogger(__name__)
DOMAIN = "eso"
CONF_OBJECTS = "objects"
CONF_CONSUMED = "consumed"
CONF_RETURNED = "returned"
CONF_COST = "cost"
CONF_PRICE_ENTITY = "price_entity"
CONF_PRICE_CURRENCY = "price_currency"
CONF_IMAP = "imap"
CONF_IMAP_HOST = "host"
CONF_IMAP_PORT = "port"
CONF_IMAP_SENDER = "sender"
CONF_IMAP_FOLDER = "folder"
DATA_DAILY_IMPORT_CANCEL = "daily_import_cancel"
SESSION_FILE = "eso_session.json"
POWER_CONSUMED = "P+"
POWER_RETURNED = "P-"
ENERGY_TYPE_MAP = {
    CONF_CONSUMED: POWER_CONSUMED,
    CONF_RETURNED: POWER_RETURNED
}
OBJECT_SCHEMA = vol.Schema({
    vol.Required(CONF_NAME): cv.string,
    vol.Required(CONF_ID): cv.string,
    vol.Required(CONF_CONSUMED, default=True): cv.boolean,
    vol.Required(CONF_RETURNED, default=False): cv.boolean,
    vol.Optional(CONF_PRICE_ENTITY): cv.string,
    vol.Optional(CONF_PRICE_CURRENCY, default="EUR"): cv.string,
})
IMAP_SCHEMA = vol.Schema({
    vol.Required(CONF_IMAP_HOST, default="imap.gmail.com"): cv.string,
    vol.Required(CONF_IMAP_PORT, default=993): cv.port,
    vol.Required(CONF_USERNAME): cv.string,
    vol.Required(CONF_PASSWORD): cv.string,
    vol.Optional(CONF_IMAP_SENDER, default="savitarna@eso.lt"): cv.string,
    vol.Optional(CONF_IMAP_FOLDER, default="INBOX"): cv.string,
})
CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Required(CONF_OBJECTS): cv.ensure_list(OBJECT_SCHEMA),
        vol.Optional(CONF_IMAP): IMAP_SCHEMA,
    })
}, extra=vol.ALLOW_EXTRA)

RETRY_DELAY_SECONDS = 3 * 3600  # 3 valandų pauzė tarp retry
DAILY_IMPORT_WINDOW_START_HOUR = 5
DAILY_IMPORT_WINDOW_START_MINUTE = 10
DAILY_IMPORT_WINDOW_SECONDS = 2 * 3600


def _random_daily_import_time(now: datetime) -> datetime:
    start = now.replace(
        hour=DAILY_IMPORT_WINDOW_START_HOUR,
        minute=DAILY_IMPORT_WINDOW_START_MINUTE,
        second=0,
        microsecond=0,
    )
    if now >= start:
        start += timedelta(days=1)
    return start + timedelta(seconds=random.randint(0, DAILY_IMPORT_WINDOW_SECONDS))


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    if DOMAIN not in config:
        return True
    domain_data = hass.data.setdefault(DOMAIN, {})
    previous_daily_import_cancel = domain_data.pop(DATA_DAILY_IMPORT_CANCEL, None)
    if previous_daily_import_cancel:
        previous_daily_import_cancel()
    imap_config = config[DOMAIN].get(CONF_IMAP)
    if imap_config:
        imap_config = {
            "host": imap_config[CONF_IMAP_HOST],
            "port": imap_config[CONF_IMAP_PORT],
            "username": imap_config[CONF_USERNAME],
            "password": imap_config[CONF_PASSWORD],
            "sender": imap_config[CONF_IMAP_SENDER],
            "folder": imap_config[CONF_IMAP_FOLDER],
        }
    client = ESOClient(
        username=config[DOMAIN][CONF_USERNAME],
        password=config[DOMAIN][CONF_PASSWORD],
        imap_config=imap_config,
        session_file=hass.config.path(SESSION_FILE),
    )

    async def async_import_generation(now: datetime, retry: bool = False) -> None:
        if hass.is_stopping:
            _LOGGER.debug("HA is stopping, skipping generation import")
            return
        all_failed = False
        try:
            _LOGGER.info(f"Logging in to ESO...")
            await hass.async_add_executor_job(client.login)
        except Exception as e:
            _LOGGER.error(f"ESO login error: {e}")
            all_failed = True
        for obj in config[DOMAIN][CONF_OBJECTS]:
            _LOGGER.info(f"Fetching ESO dataset [{obj[CONF_NAME]}]")
            try:
                await hass.async_add_executor_job(
                    client.fetch_dataset,
                    obj[CONF_ID],
                    now
                )
            except Exception as e:
                _LOGGER.error(f"ESO fetch dataset error [{obj[CONF_NAME]}]: {e}")
                all_failed = True
                continue
            dataset = client.get_dataset(obj[CONF_ID])
            await async_insert_statistics(hass, obj, dataset)
            if CONF_PRICE_ENTITY in obj and obj[CONF_PRICE_ENTITY]:
                await async_insert_cost_statistics(hass, obj, dataset)
            _LOGGER.info(f"Import completed for {obj[CONF_NAME]}")
        if all_failed and not retry:
            _LOGGER.warning("Fetch failed, will retry later")
            hass.loop.call_later(RETRY_DELAY_SECONDS, lambda: asyncio.create_task(async_import_generation(datetime.now(), retry=True)))
        elif all_failed and retry:
            _LOGGER.error("Fetch failed, postponing fetch for next day")

    daily_import_cancel = None

    def schedule_daily_import(now: datetime) -> None:
        nonlocal daily_import_cancel
        if daily_import_cancel:
            daily_import_cancel()
        next_run = _random_daily_import_time(now)
        daily_import_cancel = async_track_point_in_time(
            hass,
            async_run_scheduled_import,
            next_run,
        )
        domain_data[DATA_DAILY_IMPORT_CANCEL] = daily_import_cancel
        _LOGGER.info("Next ESO import scheduled for %s", next_run.isoformat())

    async def async_run_scheduled_import(now: datetime) -> None:
        nonlocal daily_import_cancel
        daily_import_cancel = None
        domain_data.pop(DATA_DAILY_IMPORT_CANCEL, None)
        await async_import_generation(now)
        if not hass.is_stopping:
            schedule_daily_import(now)

    # No fetch after restart; run once daily at a random time in the morning window.
    schedule_daily_import(dt_util.now())
    return True

async def async_insert_statistics(
    hass: HomeAssistant, obj: dict, dataset: dict
) -> None:
    for data_type in [CONF_CONSUMED, CONF_RETURNED]:
        if obj[data_type] is False:
            continue
        statistic_id = f"{DOMAIN}:energy_{data_type}_{obj[CONF_ID]}"
        _LOGGER.debug(f"Statistic ID for {obj[CONF_NAME]} is {statistic_id}")
        mapped_consumption_type = ENERGY_TYPE_MAP[data_type]
        if not dataset or mapped_consumption_type not in dataset:
            _LOGGER.error(f"Received empty generation data for {statistic_id}")
            continue
        generation_data = dataset[mapped_consumption_type]
        _LOGGER.debug(f"Received ESO data for {statistic_id}: {generation_data}")
        metadata = StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name=f"{obj[CONF_NAME]} ({data_type})",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            unit_class="energy",
        )
        _LOGGER.debug(f"Preparing long-term statistics for {statistic_id}")
        statistics = await _async_get_statistics(hass, metadata, generation_data)
        _LOGGER.debug(f"Generated statistics for {statistic_id}: {statistics}")
        async_add_external_statistics(hass, metadata, statistics)

async def _async_get_statistics(hass: HomeAssistant, metadata: StatisticMetaData, generation_data: dict) -> list[StatisticData]:
    statistics: list[StatisticData] = []
    sum_ = None
    for ts, kwh in generation_data.items():
        dt_object = datetime.fromtimestamp(ts).replace(tzinfo=dt_util.get_time_zone("Europe/Vilnius"))
        if sum_ is None:
            sum_ = await get_previous_sum(hass, metadata, dt_object)
        sum_ += kwh
        statistics.append(
            StatisticData(
                start=dt_object,
                state=kwh,
                sum=sum_
            )
        )
    return statistics

async def get_previous_sum(hass: HomeAssistant, metadata: StatisticMetaData, date: datetime) -> float:
    # Look back far enough to survive multi-day fetch failures and take the most
    # recent point before `date`. A 1-hour lookup silently resets the cumulative
    # sum to 0 whenever a gap appears (e.g. failed imports), which corrupts the
    # long-term energy statistics from that point on.
    statistic_id = metadata["statistic_id"]
    start = date - timedelta(days=60)
    end = date
    _LOGGER.debug(f"Looking history sum for {statistic_id} for {date} between {start} and {end}")
    stat = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, {statistic_id}, "hour", None, {"sum"}
    )
    rows = stat.get(statistic_id) if stat else None
    if not rows:
        _LOGGER.debug(f"No history sum found")
        return 0.0
    sum_ = rows[-1].get("sum") or 0.0
    _LOGGER.debug(f"History sum for {statistic_id} = {sum_}")
    return sum_

async def async_insert_cost_statistics(
    hass: HomeAssistant, obj: dict, consumption_dataset: dict
) -> None:
    if obj[CONF_CONSUMED] is False:
        return
    cons_dataset = consumption_dataset[ENERGY_TYPE_MAP[CONF_CONSUMED]]
    start_time = datetime.fromtimestamp(min(cons_dataset.keys())).replace(tzinfo=dt_util.get_time_zone("Europe/Vilnius"))
    end_time = datetime.fromtimestamp(max(cons_dataset.keys())).replace(tzinfo=dt_util.get_time_zone("Europe/Vilnius"))
    prices = await _async_generate_price_dict(hass, obj, start_time, end_time)
    if prices is None:
        return
    cost_metadata = StatisticMetaData(
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
        name=f"{obj[CONF_NAME]} ({CONF_COST})",
        source=DOMAIN,
        statistic_id=f"{DOMAIN}:energy_{CONF_COST}_{obj[CONF_ID]}",
        unit_of_measurement=obj[CONF_PRICE_CURRENCY],
        unit_class=None,
    )
    cost_stats: list[StatisticData] = []
    cost_sum_ = None
    for ts, cons_kwh in cons_dataset.items():
        dt_object = datetime.fromtimestamp(ts).replace(tzinfo=dt_util.get_time_zone("Europe/Vilnius"))
        price = prices.get(ts, 0)
        cost = round(cons_kwh * price, 5)
        if cost_sum_ is None:
            cost_sum_ = await get_previous_sum(hass, cost_metadata, start_time)
        cost_sum_ += cost
        cost_stats.append(
            StatisticData(
                start=dt_object,
                state=cost,
                sum=cost_sum_,
            )
        )
    _LOGGER.debug(f"Generated cost statistics for {DOMAIN}:energy_{CONF_COST}_{obj[CONF_ID]}: {cost_stats}")
    async_add_external_statistics(hass, cost_metadata, cost_stats)

async def _async_generate_price_dict(
    hass: HomeAssistant, obj: dict, time_from: datetime, time_to: datetime
) -> dict:
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, time_from, time_to, {obj[CONF_PRICE_ENTITY]}, "hour", None, {"state"}
    )
    price_stats = stats.get(obj[CONF_PRICE_ENTITY])
    if price_stats is None:
        _LOGGER.warning(
            "No price statistics for %s between %s and %s", obj[CONF_PRICE_ENTITY], time_from.isoformat(), time_to.isoformat()
        )
        return {}
    _LOGGER.debug(
        "Retrieving price statistics for %s between %s and %s: %s", obj[CONF_PRICE_ENTITY], time_from, time_to, price_stats
    )
    prices = {}
    for rec in price_stats:
        prices[rec["start"]] = rec["state"]
    return prices
