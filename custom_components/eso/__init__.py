import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.const import (
    CONF_ID,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_USERNAME,
    UnitOfEnergy,
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_point_in_time
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DATE,
    CONF_CONSUMED,
    CONF_COST,
    CONF_EXPORT_BALANCE,
    CONF_FIXED_PRICE,
    CONF_IMAP,
    CONF_IMAP_FOLDER,
    CONF_IMAP_HOST,
    CONF_IMAP_PORT,
    CONF_IMAP_SENDER,
    CONF_OBJECTS,
    CONF_PRICE_CURRENCY,
    CONF_PRICE_ENTITY,
    CONF_PROVIDER,
    CONF_RETURNED,
    DAILY_IMPORT_WINDOW_SECONDS,
    DAILY_IMPORT_WINDOW_START_HOUR,
    DAILY_IMPORT_WINDOW_START_MINUTE,
    DEFAULT_IMAP_FOLDER,
    DEFAULT_IMAP_HOST,
    DEFAULT_IMAP_PORT,
    DEFAULT_IMAP_SENDER,
    DEFAULT_PRICE_CURRENCY,
    DEFAULT_PROVIDER,
    DOMAIN,
    ENERGY_TYPE_MAP,
    EXPORT_BALANCE_KEY,
    IGNITIS_IMPORT_HOUR,
    IGNITIS_IMPORT_MINUTE,
    IGNITIS_MAX_RETRIES,
    IGNITIS_RETRY_DELAY_SECONDS,
    PROVIDER_IGNITIS,
    PROVIDERS,
    RETRY_DELAY_SECONDS,
    SERVICE_IMPORT_NOW,
    SESSION_FILE,
    SUBENTRY_TYPE_OBJECT,
    TIMEZONE,
)
from .eso_client import ESOAuthError, ESOClient
from .ignitis_client import IgnitisClient

_LOGGER = logging.getLogger(__name__)


@dataclass
class ESORuntimeData:
    """Runtime data stored on the config entry."""

    client: ESOClient | IgnitisClient
    async_import: Callable[[datetime], Awaitable[None]]


type ESOConfigEntry = ConfigEntry[ESORuntimeData]

OBJECT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_ID): cv.string,
        vol.Required(CONF_CONSUMED, default=True): cv.boolean,
        vol.Required(CONF_RETURNED, default=False): cv.boolean,
        vol.Optional(CONF_PRICE_ENTITY): cv.string,
        vol.Optional(CONF_PRICE_CURRENCY, default=DEFAULT_PRICE_CURRENCY): cv.string,
        vol.Optional(CONF_FIXED_PRICE): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Required(CONF_EXPORT_BALANCE, default=False): cv.boolean,
    }
)
IMAP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_IMAP_HOST, default=DEFAULT_IMAP_HOST): cv.string,
        vol.Required(CONF_IMAP_PORT, default=DEFAULT_IMAP_PORT): cv.port,
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_IMAP_SENDER, default=DEFAULT_IMAP_SENDER): cv.string,
        vol.Optional(CONF_IMAP_FOLDER, default=DEFAULT_IMAP_FOLDER): cv.string,
    }
)
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_USERNAME): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
                vol.Optional(CONF_PROVIDER, default=DEFAULT_PROVIDER): vol.In(PROVIDERS),
                vol.Required(CONF_OBJECTS): cv.ensure_list(OBJECT_SCHEMA),
                vol.Optional(CONF_IMAP): IMAP_SCHEMA,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

SERVICE_IMPORT_NOW_SCHEMA = vol.Schema(
    {vol.Optional(ATTR_CONFIG_ENTRY_ID): vol.All(cv.ensure_list, [cv.string]), vol.Optional(ATTR_DATE): cv.datetime}
)


def _next_import_time(now: datetime, provider: str) -> datetime:
    if provider == PROVIDER_IGNITIS:
        start = now.replace(
            hour=IGNITIS_IMPORT_HOUR,
            minute=IGNITIS_IMPORT_MINUTE,
            second=0,
            microsecond=0,
        )
        if now >= start:
            start += timedelta(days=1)
        return start

    start = now.replace(
        hour=DAILY_IMPORT_WINDOW_START_HOUR,
        minute=DAILY_IMPORT_WINDOW_START_MINUTE,
        second=0,
        microsecond=0,
    )
    if now >= start:
        start += timedelta(days=1)
    return start + timedelta(seconds=random.randint(0, DAILY_IMPORT_WINDOW_SECONDS))


def _expected_hourly_points(day: date) -> int:
    tz = dt_util.get_time_zone(TIMEZONE)
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    nxt = start + timedelta(days=1)
    return round((nxt.timestamp() - start.timestamp()) / 3600)


def _need_retry(dataset: dict | None, target_day: date) -> bool:
    if not dataset:
        return True
    expected = _expected_hourly_points(target_day)
    for data_type in [CONF_CONSUMED, CONF_RETURNED]:
        if len(dataset.get(ENERGY_TYPE_MAP[data_type], {})) < expected:
            return True
    return False


def _entry_objects(entry: ESOConfigEntry) -> list[dict]:
    """Return the configured objects (one per subentry) as object dicts."""
    return [
        dict(subentry.data)
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TYPE_OBJECT
    ]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Import a legacy YAML configuration into a config entry."""
    if DOMAIN not in config:
        return True
    _LOGGER.warning(
        "Configuring the ESO integration via configuration.yaml is deprecated and will be removed in a future release."
        "Your existing YAML configuration has been imported into the UI; remove the `eso:` block from configuration.yaml."
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        "deprecated_yaml",
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="deprecated_yaml",
    )
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=dict(config[DOMAIN])
        )
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ESOConfigEntry) -> bool:
    """Set up ESO from a config entry."""
    provider = entry.data.get(CONF_PROVIDER, DEFAULT_PROVIDER)

    if provider == PROVIDER_IGNITIS:
        client: ESOClient | IgnitisClient = IgnitisClient(
            username=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
        )
    else:
        if not entry.data.get(CONF_IMAP):
            raise ConfigEntryAuthFailed(
                "A mailbox (IMAP) is required for ESO two-factor login but is not configured."
                "Please reconfigure the integration."
            )

        imap_config_data = entry.data.get(CONF_IMAP)
        imap_config = {
            "host": imap_config_data[CONF_IMAP_HOST],
            "port": imap_config_data[CONF_IMAP_PORT],
            "username": imap_config_data[CONF_USERNAME],
            "password": imap_config_data[CONF_PASSWORD],
            "sender": imap_config_data.get(CONF_IMAP_SENDER, DEFAULT_IMAP_SENDER),
            "folder": imap_config_data.get(CONF_IMAP_FOLDER, DEFAULT_IMAP_FOLDER),
        }

        client = ESOClient(
            username=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
            imap_config=imap_config,
            session_file=hass.config.path(SESSION_FILE),
        )

    retry_delay = (
        IGNITIS_RETRY_DELAY_SECONDS
        if provider == PROVIDER_IGNITIS
        else RETRY_DELAY_SECONDS
    )
    max_retries = IGNITIS_MAX_RETRIES if provider == PROVIDER_IGNITIS else 1

    async def async_import_generation(now: datetime, retry: int = 0) -> None:
        if hass.is_stopping:
            _LOGGER.debug("HA is stopping, skipping generation import")
            return
        objects = _entry_objects(entry)
        all_failed = False
        auth_failed = False
        try:
            _LOGGER.info("Logging in to %s...", provider.upper())
            await hass.async_add_executor_job(client.login)
        except ESOAuthError as err:
            _LOGGER.error("Authentication failed: %s. Reconfigure the integration to update credentials.", err)
            auth_failed = True
        except Exception as err:
            _LOGGER.error("ESO login error: %s", err)
            all_failed = True
        for obj in objects if not auth_failed else []:
            _LOGGER.info("Fetching ESO dataset [%s]", obj[CONF_NAME])
            try:
                await hass.async_add_executor_job(client.fetch_dataset, obj[CONF_ID], now)
            except ESOAuthError as err:
                _LOGGER.error("Authentication failed for %s: %s. Reconfigure the integration to update credentials.", obj[CONF_NAME], err)
                auth_failed = True
                break
            except Exception as err:
                _LOGGER.error("ESO fetch dataset error [%s]: %s", obj[CONF_NAME], err)
                all_failed = True
                continue
            dataset = client.get_dataset(obj[CONF_ID])
            target_day = (now - timedelta(days=1)).date()
            if provider == PROVIDER_IGNITIS and _need_retry(dataset, target_day):
                _LOGGER.warning("Received incomplete data for %s, will retry later", obj[CONF_NAME])
                all_failed = True
                continue
            await async_insert_statistics(hass, obj, dataset)
            if obj.get(CONF_PRICE_ENTITY):
                await async_insert_cost_statistics(hass, obj, dataset)
            elif obj.get(CONF_FIXED_PRICE) is not None:
                await async_insert_fixed_price_cost_statistics(hass, obj, dataset)
            if obj.get(CONF_EXPORT_BALANCE):
                await async_insert_export_balance_statistics(hass, obj, dataset)
            _LOGGER.info("Import completed for %s", obj[CONF_NAME])
        if auth_failed:
            return
        if all_failed and retry < max_retries:
            retry_at = dt_util.now() + timedelta(seconds=retry_delay)
            _LOGGER.warning("Fetch failed, will retry at %s (attempt %d/%d)", retry_at.isoformat(), retry + 1, max_retries)

            async def _retry(_now: datetime) -> None:
                await async_import_generation(now, retry=retry + 1)

            entry.async_on_unload(async_call_later(hass, retry_delay, _retry))
        elif all_failed:
            _LOGGER.error("Fetch failed, postponing fetch for next day")

    daily_import_cancel = None

    def schedule_daily_import(now: datetime) -> None:
        nonlocal daily_import_cancel
        if daily_import_cancel:
            daily_import_cancel()
        next_run = _next_import_time(now, provider)
        daily_import_cancel = async_track_point_in_time(
            hass,
            async_run_scheduled_import,
            next_run,
        )
        _LOGGER.info("Next daily ESO import scheduled for %s", next_run.isoformat())

    async def async_run_scheduled_import(now: datetime) -> None:
        nonlocal daily_import_cancel
        daily_import_cancel = None
        await async_import_generation(now)
        if not hass.is_stopping:
            schedule_daily_import(now)

    schedule_daily_import(dt_util.now())
    entry.async_on_unload(lambda: daily_import_cancel and daily_import_cancel())
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    entry.runtime_data = ESORuntimeData(
        client=client,
        async_import=async_import_generation,
    )
    _async_register_services(hass)
    return True


def _async_register_services(hass: HomeAssistant) -> None:
    """Register integration services once."""
    if hass.services.has_service(DOMAIN, SERVICE_IMPORT_NOW):
        return

    async def async_handle_import_now(call: ServiceCall) -> None:
        entries: list[ESOConfigEntry] = hass.config_entries.async_loaded_entries(DOMAIN)
        by_id = {entry.entry_id: entry for entry in entries}
        entry_ids = call.data.get(ATTR_CONFIG_ENTRY_ID)
        if entry_ids:
            unknown = [eid for eid in entry_ids if eid not in by_id]
            if unknown:
                raise ServiceValidationError(
                    f"Unknown ESO config entry id(s): {', '.join(unknown)}"
                )
            targets = [by_id[eid].runtime_data.async_import for eid in entry_ids]
        else:
            targets = [entry.runtime_data.async_import for entry in entries]
        if not targets:
            raise ServiceValidationError("No ESO accounts are configured")
        reference = call.data.get(ATTR_DATE)
        if reference is None:
            reference = dt_util.now()
        elif reference.tzinfo is None:
            reference = reference.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        _LOGGER.info("ESO: on-demand import requested for %d account(s) as of %s", len(targets), reference.isoformat())
        for callback in targets:
            await callback(reference)

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_NOW,
        async_handle_import_now,
        schema=SERVICE_IMPORT_NOW_SCHEMA,
    )


async def async_unload_entry(hass: HomeAssistant, entry: ESOConfigEntry) -> bool:
    """Unload a config entry (scheduling is torn down via async_on_unload)."""
    remaining = [
        other
        for other in hass.config_entries.async_loaded_entries(DOMAIN)
        if other.entry_id != entry.entry_id
    ]
    if not remaining and hass.services.has_service(DOMAIN, SERVICE_IMPORT_NOW):
        hass.services.async_remove(DOMAIN, SERVICE_IMPORT_NOW)
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ESOConfigEntry) -> None:
    """Reload the config entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_insert_statistics(
    hass: HomeAssistant, obj: dict, dataset: dict
) -> None:
    for data_type in [CONF_CONSUMED, CONF_RETURNED]:
        if obj.get(data_type) is False:
            continue
        statistic_id = f"{DOMAIN}:energy_{data_type}_{obj[CONF_ID]}"
        _LOGGER.debug("Statistic ID for %s is %s", obj[CONF_NAME], statistic_id)
        mapped_consumption_type = ENERGY_TYPE_MAP[data_type]
        if not dataset or mapped_consumption_type not in dataset:
            _LOGGER.error("Received empty generation data for %s", statistic_id)
            continue
        generation_data = dataset[mapped_consumption_type]
        _LOGGER.debug("Received ESO data for %s: %s", statistic_id, generation_data)
        metadata = StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name=f"{obj[CONF_NAME]} ({data_type})",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            unit_class="energy",
        )
        _LOGGER.debug("Preparing long-term statistics for %s", statistic_id)
        statistics = await _async_get_statistics(hass, metadata, generation_data)
        _LOGGER.debug("Generated statistics for %s: %s", statistic_id, statistics)
        async_add_external_statistics(hass, metadata, statistics)


async def _async_get_statistics(
    hass: HomeAssistant,
    metadata: StatisticMetaData,
    generation_data: dict,
) -> list[StatisticData]:
    statistics: list[StatisticData] = []
    sum_ = None
    for ts, kwh in generation_data.items():
        dt_object = datetime.fromtimestamp(ts).replace(
            tzinfo=dt_util.get_time_zone(TIMEZONE)
        )
        if sum_ is None:
            sum_ = await get_previous_sum(hass, metadata, dt_object)
        sum_ += kwh
        statistics.append(
            StatisticData(
                start=dt_object,
                state=kwh,
                sum=sum_,
            )
        )
    return statistics


async def get_previous_sum(
    hass: HomeAssistant,
    metadata: StatisticMetaData,
    date: datetime,
) -> float:
    # Look back far enough to survive multi-day fetch failures and take the most
    # recent point before `date`. A 1-hour lookup silently resets the cumulative
    # sum to 0 whenever a gap appears, which corrupts the long-term statistics.
    statistic_id = metadata["statistic_id"]
    start = date - timedelta(days=60)
    end = date
    _LOGGER.debug(
        "Looking history sum for %s for %s between %s and %s",
        statistic_id,
        date,
        start,
        end,
    )
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
    rows = stat.get(statistic_id) if stat else None
    if not rows:
        _LOGGER.debug("No history sum found")
        return 0.0
    sum_ = rows[-1].get("sum") or 0.0
    _LOGGER.debug("History sum for %s = %s", statistic_id, sum_)
    return sum_


async def async_insert_cost_statistics(
    hass: HomeAssistant,
    obj: dict,
    consumption_dataset: dict,
) -> None:
    if obj.get(CONF_CONSUMED) is False:
        return
    series = consumption_dataset.get(ENERGY_TYPE_MAP[CONF_CONSUMED])
    if not series:
        return
    start_time = datetime.fromtimestamp(min(series.keys())).replace(
        tzinfo=dt_util.get_time_zone(TIMEZONE)
    )
    end_time = datetime.fromtimestamp(max(series.keys())).replace(
        tzinfo=dt_util.get_time_zone(TIMEZONE)
    )
    prices = await _async_generate_price_dict(hass, obj, start_time, end_time)

    def price_for(ts: float) -> float:
        return prices.get(ts, 0)

    await _async_insert_cost_series(
        hass,
        obj,
        f"{DOMAIN}:energy_{CONF_COST}_{obj[CONF_ID]}",
        f"{obj[CONF_NAME]} ({CONF_COST})",
        series,
        price_for,
    )


async def async_insert_fixed_price_cost_statistics(
    hass: HomeAssistant,
    obj: dict,
    consumption_dataset: dict,
) -> None:
    fixed_price = obj.get(CONF_FIXED_PRICE)
    if fixed_price is None:
        return

    def price_for(ts: float) -> float:
        return fixed_price

    for data_type in [CONF_CONSUMED, CONF_RETURNED]:
        if obj.get(data_type) is False:
            continue
        series = consumption_dataset.get(ENERGY_TYPE_MAP[data_type])
        if not series:
            continue
        await _async_insert_cost_series(
            hass,
            obj,
            f"{DOMAIN}:energy_{CONF_COST}_{data_type}_{obj[CONF_ID]}",
            f"{obj[CONF_NAME]} {data_type} ({CONF_COST})",
            series,
            price_for,
        )


async def _async_insert_cost_series(
    hass: HomeAssistant,
    obj: dict,
    statistic_id: str,
    name: str,
    series: dict,
    price_for: Callable[[float], float],
) -> None:
    cost_metadata = StatisticMetaData(
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement=obj.get(CONF_PRICE_CURRENCY, DEFAULT_PRICE_CURRENCY),
        unit_class=None,
    )
    cost_stats: list[StatisticData] = []
    cost_sum_ = None
    for ts, kwh in series.items():
        dt_object = datetime.fromtimestamp(ts).replace(
            tzinfo=dt_util.get_time_zone(TIMEZONE)
        )
        cost = round(kwh * price_for(ts), 5)
        if cost_sum_ is None:
            cost_sum_ = await get_previous_sum(hass, cost_metadata, dt_object)
        cost_sum_ += cost
        cost_stats.append(StatisticData(start=dt_object, state=cost, sum=cost_sum_))
    _LOGGER.debug(
        "Generated cost statistics for %s: %s",
        statistic_id,
        cost_stats,
    )
    async_add_external_statistics(hass, cost_metadata, cost_stats)


async def async_insert_export_balance_statistics(
    hass: HomeAssistant,
    obj: dict,
    consumption_dataset: dict,
) -> None:
    balance = consumption_dataset.get(EXPORT_BALANCE_KEY) if consumption_dataset else None
    if balance is None:
        _LOGGER.warning("Received empty export balance data for %s", obj[CONF_NAME])
        return
    statistic_id = f"{DOMAIN}:energy_{CONF_EXPORT_BALANCE}_{obj[CONF_ID]}"
    metadata = StatisticMetaData(
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
        name=f"{obj[CONF_NAME]} ({CONF_EXPORT_BALANCE})",
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        unit_class="energy",
    )
    tz = dt_util.get_time_zone(TIMEZONE)
    timestamps = list(consumption_dataset.get(ENERGY_TYPE_MAP[CONF_CONSUMED], {}))
    timestamps += list(consumption_dataset.get(ENERGY_TYPE_MAP[CONF_RETURNED], {}))
    if timestamps:
        start = datetime.fromtimestamp(max(timestamps)).replace(tzinfo=tz)
    else:
        start = datetime.now(tz=tz).replace(
            minute=0, second=0, microsecond=0
        ) - timedelta(hours=1)
    statistics = [StatisticData(start=start, state=balance, sum=balance)]
    _LOGGER.debug("Generated export balance statistics for %s: %s", statistic_id, statistics)
    async_add_external_statistics(hass, metadata, statistics)


async def _async_generate_price_dict(
    hass: HomeAssistant,
    obj: dict,
    time_from: datetime,
    time_to: datetime,
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
