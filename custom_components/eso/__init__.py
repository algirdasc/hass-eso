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
CONF_COST = "cost"
CONF_PRICE_ENTITY = "price_entity"
CONF_PRICE_CURRENCY = "price_currency"

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
        vol.Optional(CONF_PRICE_ENTITY): cv.string,
        vol.Optional(CONF_PRICE_CURRENCY, default="EUR"): cv.string,
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
            _LOGGER.debug(f"Fetching {DOMAIN} data for {obj[CONF_NAME]}")
            await hass.async_add_executor_job(
                client.fetch_dataset,
                obj[CONF_ID],
                now
            )

            _LOGGER.debug(f"Importing {DOMAIN} data for {obj[CONF_NAME]}")
            await async_insert_statistics(hass, obj, client.get_dataset(obj[CONF_ID]))

            if CONF_PRICE_ENTITY in obj and obj[CONF_PRICE_ENTITY]:
                await async_insert_cost_statistics(
                    hass, obj, client.get_dataset(obj[CONF_ID])
                )

        _LOGGER.debug(f"Imported {DOMAIN} data")

    async def async_first_start(event: Event) -> None:
        await async_import_generation(datetime.now())

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, async_first_start)

    async_track_time_interval(hass, async_import_generation, timedelta(hours=2))

    return True


async def async_insert_statistics(
        hass: HomeAssistant,
        obj: dict,
        dataset: dict
) -> None:
    for data_type in [CONF_CONSUMED, CONF_RETURNED]:
        if obj[data_type] is False:
            continue

        statistic_id = f"{DOMAIN}:energy_{data_type}_{obj[CONF_ID]}"

        _LOGGER.debug(f"Statistic ID for {obj[CONF_NAME]} is {statistic_id}")

        mapped_consumption_type = ENERGY_TYPE_MAP[data_type]

        if not dataset or mapped_consumption_type not in dataset:
            _LOGGER.error(f"Received empty generation data for {statistic_id}")
            return None

        generation_data = dataset[mapped_consumption_type]

        _LOGGER.debug(f"Received {DOMAIN} data for {statistic_id}: {generation_data}")

        metadata = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name=f"{obj[CONF_NAME]} ({data_type})",
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
    statistic_id = metadata["statistic_id"]
    start = date - timedelta(hours=1)
    end = date

    _LOGGER.debug(f"Looking history sum for {statistic_id} for {date} between {start} and {end}")

    stat = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )

    if statistic_id not in stat:
        _LOGGER.debug(f"No history sum found")
        return 0.0

    sum_ = stat[statistic_id][0]["sum"]

    _LOGGER.debug(f"History sum for {statistic_id} = {sum_}")

    return sum_


async def async_insert_cost_statistics(
        hass: HomeAssistant,
        obj: dict,
        consumption_dataset: dict
) -> None:
    if obj[CONF_CONSUMED] is False:
        return

    cons_dataset = consumption_dataset[ENERGY_TYPE_MAP[CONF_CONSUMED]]
    start_time = dt_util.fromtimestamp(min(cons_dataset.keys())).replace(tzinfo=dt_util.get_time_zone("Europe/Vilnius"))
    end_time = dt_util.fromtimestamp(max(cons_dataset.keys())).replace(tzinfo=dt_util.get_time_zone("Europe/Vilnius"))

    prices = await _async_generate_price_dict(hass, obj, start_time, end_time)

    if prices is None:
        return

    cost_metadata = StatisticMetaData(
        has_mean=False,
        has_sum=True,
        name=f"{obj[CONF_NAME]} ({CONF_COST})",
        source=DOMAIN,
        statistic_id=f"{DOMAIN}:energy_{CONF_COST}_{obj[CONF_ID]}",
        unit_of_measurement=obj[CONF_PRICE_CURRENCY],
    )

    cost_stats: list[StatisticData] = []
    cost_sum_ = None

    for ts, cons_kwh in cons_dataset.items():
        dt_object = datetime.fromtimestamp(ts).replace(tzinfo=dt_util.get_time_zone("Europe/Vilnius"))

        # Decided to support zero price and therefore produce 0 cost
        price = prices.get(ts, 0)

        # Ignitis rounds hourly costs to 5 decimal places
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

    _LOGGER.debug(
        f"Generated cost statistics for {DOMAIN}:energy_{CONF_COST}_{obj[CONF_ID]}: {cost_stats}"
    )
    async_add_external_statistics(hass, cost_metadata, cost_stats)


async def _async_generate_price_dict(
        hass: HomeAssistant,
        obj: dict,
        time_from: datetime,
        time_to: datetime
) -> dict:
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        time_from,
        time_to,
        {obj[CONF_PRICE_ENTITY]},
        "hour",
        None,
        {"state"},
    )

    price_stats = stats.get(obj[CONF_PRICE_ENTITY])

    if price_stats is None:
        _LOGGER.warning(
            "No price statistics for %s between %s and %s",
            obj[CONF_PRICE_ENTITY],
            time_from.isoformat(),
            time_to.isoformat(),
        )

        return {}

    _LOGGER.debug(
        "Retrieving price statistics for %s between %s and %s: %s",
        obj[CONF_PRICE_ENTITY],
        time_from,
        time_to,
        price_stats,
    )

    prices = {}
    for rec in price_stats:
        prices[rec["start"]] = rec["state"]

    return prices
