import logging
from datetime import timedelta, datetime

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMetaData, StatisticData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.const import (
    CONF_ID,
    CONF_NAME,
    CONF_USERNAME,
    CONF_PASSWORD,
    UnitOfEnergy, EVENT_HOMEASSISTANT_STARTED
)
from homeassistant.core import HomeAssistant, Event
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .eso_client import ESOClient

_LOGGER = logging.getLogger(__name__)

DOMAIN = "eso"

CONF_OBJECTS = "objects"
CONF_CONSUMED = "consumed"
CONF_RETURNED = "returned"

POWER_CONSUMED = "P+"
POWER_RETURNED = "P-"

ENERGY_TYPE_MAP = {
    CONF_CONSUMED: POWER_CONSUMED,
    CONF_RETURNED: POWER_RETURNED
}

OBJECT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_ID): cv.string,
        vol.Required(CONF_CONSUMED, default=True): cv.boolean,
        vol.Required(CONF_RETURNED, default=False): cv.boolean,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_USERNAME): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
                vol.Required(CONF_OBJECTS): cv.ensure_list(OBJECT_SCHEMA),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    if DOMAIN not in config:
        return True

    hass.data.setdefault(DOMAIN, config[DOMAIN])

    client = ESOClient(
        username=config[DOMAIN][CONF_USERNAME],
        password=config[DOMAIN][CONF_PASSWORD]
    )

    async def async_import_generation(now: datetime) -> None:
        if hass.is_stopping:
            _LOGGER.debug("HA is stopping, skipping generation import")
            return

        _LOGGER.debug(f"Logging in to {DOMAIN} site")
        await hass.async_add_executor_job(client.login)

        for obj in config[DOMAIN][CONF_OBJECTS]:
            _LOGGER.debug(f"Fetching consumption data for {obj[CONF_NAME]}")
            await hass.async_add_executor_job(
                client.fetch_consumption_data,
                obj[CONF_ID],
                now
            )

            _LOGGER.debug(f"Importing generation data for {obj[CONF_NAME]}")
            await async_insert_statistics(
                hass,
                obj,
                client.get_consumption_data(obj[CONF_ID])
            )

        _LOGGER.debug(f"Imported consumption data")

    async def async_first_start(event: Event) -> None:
        await async_import_generation(datetime.now())

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, async_first_start)

    async_track_time_interval(hass, async_import_generation, timedelta(hours=2))

    return True


async def async_insert_statistics(
        hass: HomeAssistant,
        obj: dict,
        generation_data: dict
) -> None:
    for consumption_type in [CONF_CONSUMED, CONF_RETURNED]:
        if obj[consumption_type] is False:
            continue

        statistic_id = f"{DOMAIN}:energy_{consumption_type}_{obj[CONF_ID]}"

        _LOGGER.debug(f"Statistic ID for {obj[CONF_NAME]} is {statistic_id}")

        mapped_consumption_type = ENERGY_TYPE_MAP[consumption_type]

        if not generation_data or mapped_consumption_type not in generation_data:
            _LOGGER.error(f"Received empty generation data for {statistic_id}")
            return None

        generation_data = generation_data[mapped_consumption_type]

        _LOGGER.debug(f"Received generation data for {statistic_id}: {generation_data}")

        metadata = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name=f"{obj[CONF_NAME]} ({consumption_type})",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )

        _LOGGER.debug(f"Preparing long-term statistics for {statistic_id}")

        statistics = await _async_get_statistics(hass, metadata, generation_data)

        _LOGGER.debug(f"Generated statistics for {statistic_id}: {statistics}")

        async_add_external_statistics(hass, metadata, statistics)


async def _async_get_statistics(
        hass: HomeAssistant,
        metadata: StatisticMetaData,
        generation_data: dict
) -> list[StatisticData]:
    statistics: list[StatisticData] = []
    sum_ = None

    for ts, kwh in generation_data.items():
        dt_object = datetime.fromtimestamp(ts)

        if sum_ is None:
            sum_ = await get_yesterday_sum(hass, metadata, dt_object)

        sum_ += kwh

        statistics.append(
            StatisticData(
                start=dt_object.replace(tzinfo=dt_util.get_time_zone("Europe/Vilnius")),
                state=kwh,
                sum=sum_
            )
        )

    return statistics


async def get_yesterday_sum(hass: HomeAssistant, metadata: StatisticMetaData, date: datetime) -> float:
    statistic_id = metadata["statistic_id"]
    start = date - timedelta(days=1)
    end = date - timedelta(minutes=1)

    _LOGGER.debug(f"Looking history sum for {statistic_id} for {date} between {start} and {end}")

    stat = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {statistic_id},
        "day",
        None,
        {"sum"},
    )

    if statistic_id not in stat:
        _LOGGER.debug(f"No history sum found")
        return 0.0

    sum_ = stat[statistic_id][0]["sum"]

    _LOGGER.debug(f"History sum for {statistic_id} = {sum_}")

    return sum_
