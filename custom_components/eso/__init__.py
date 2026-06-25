import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta, datetime
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
from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.const import (
    CONF_ID, CONF_NAME, CONF_USERNAME, CONF_PASSWORD, UnitOfEnergy
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    CONF_CONSUMED,
    CONF_COST,
    CONF_IMAP,
    CONF_IMAP_FOLDER,
    CONF_IMAP_HOST,
    CONF_IMAP_PORT,
    CONF_IMAP_SENDER,
    CONF_OBJECTS,
    CONF_PRICE_CURRENCY,
    CONF_PRICE_ENTITY,
    CONF_RETURNED,
    DEFAULT_IMAP_FOLDER,
    DEFAULT_IMAP_HOST,
    DEFAULT_IMAP_PORT,
    DEFAULT_IMAP_SENDER,
    DEFAULT_PRICE_CURRENCY,
    DOMAIN,
    ENERGY_TYPE_MAP,
    RETRY_DELAY_SECONDS,
    SERVICE_IMPORT_NOW,
    SESSION_FILE,
    SUBENTRY_TYPE_OBJECT,
)
from .eso_client import ESOClient

_LOGGER = logging.getLogger(__name__)

@dataclass
class ESORuntimeData:
    """Runtime data stored on the config entry."""

    client: ESOClient
    async_import: Callable[[datetime], Awaitable[None]]

type ESOConfigEntry = ConfigEntry[ESORuntimeData]

OBJECT_SCHEMA = vol.Schema({
    vol.Required(CONF_NAME): cv.string,
    vol.Required(CONF_ID): cv.string,
    vol.Required(CONF_CONSUMED, default=True): cv.boolean,
    vol.Required(CONF_RETURNED, default=False): cv.boolean,
    vol.Optional(CONF_PRICE_ENTITY): cv.string,
    vol.Optional(CONF_PRICE_CURRENCY, default=DEFAULT_PRICE_CURRENCY): cv.string,
})
IMAP_SCHEMA = vol.Schema({
    vol.Required(CONF_IMAP_HOST, default=DEFAULT_IMAP_HOST): cv.string,
    vol.Required(CONF_IMAP_PORT, default=DEFAULT_IMAP_PORT): cv.port,
    vol.Required(CONF_USERNAME): cv.string,
    vol.Required(CONF_PASSWORD): cv.string,
    vol.Optional(CONF_IMAP_SENDER, default=DEFAULT_IMAP_SENDER): cv.string,
    vol.Optional(CONF_IMAP_FOLDER, default=DEFAULT_IMAP_FOLDER): cv.string,
})
CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Required(CONF_OBJECTS): cv.ensure_list(OBJECT_SCHEMA),
        vol.Optional(CONF_IMAP): IMAP_SCHEMA,
    })
}, extra=vol.ALLOW_EXTRA)


SERVICE_IMPORT_NOW_SCHEMA = vol.Schema(
    {vol.Optional(ATTR_CONFIG_ENTRY_ID): vol.All(cv.ensure_list, [cv.string])}
)


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
    if not entry.data.get(CONF_IMAP):
        # ESO now enforces email 2FA on every login, so a mailbox is required.
        # Entries imported from older YAML (where imap was optional) lack it and cannot log in.
        # Fail setup with a reauth so the user is prompted to add it;
        # the entry shows as "needs attention" in the UI.
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

    async def async_import_generation(now: datetime, retry: bool = False) -> None:
        if hass.is_stopping:
            _LOGGER.debug("HA is stopping, skipping generation import")
            return
        objects = _entry_objects(entry)
        all_failed = False
        try:
            _LOGGER.info("Logging in to ESO...")
            await hass.async_add_executor_job(client.login)
        except Exception as e:
            _LOGGER.error(f"ESO login error: {e}")
            all_failed = True
        for obj in objects:
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
            if obj.get(CONF_PRICE_ENTITY):
                await async_insert_cost_statistics(hass, obj, dataset)
            _LOGGER.info(f"Import completed for {obj[CONF_NAME]}")
        if all_failed and not retry:
            _LOGGER.warning("Fetch failed, will retry later")

            async def _retry(_now: datetime) -> None:
                await async_import_generation(datetime.now(), retry=True)

            entry.async_on_unload(
                async_call_later(hass, RETRY_DELAY_SECONDS, _retry)
            )
        elif all_failed and retry:
            _LOGGER.error("Fetch failed, postponing fetch for next day")

    entry.async_on_unload(
        async_track_time_change(
            hass, async_import_generation, hour=5, minute=11, second=0
        )
    )
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
        entries: list[ESOConfigEntry] = hass.config_entries.async_loaded_entries(
            DOMAIN
        )
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
        _LOGGER.info("ESO: on-demand import requested for %d account(s)", len(targets))
        for callback in targets:
            await callback(datetime.now())

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_NOW,
        async_handle_import_now,
        schema=SERVICE_IMPORT_NOW_SCHEMA,
    )


async def async_unload_entry(hass: HomeAssistant, entry: ESOConfigEntry) -> bool:
    """Unload a config entry (scheduling is torn down via async_on_unload)."""
    # The entry being unloaded is no longer reported as loaded, so if no other
    # loaded entries remain we can remove the integration-wide service.
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
    if obj.get(CONF_CONSUMED) is False:
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
        unit_of_measurement=obj.get(CONF_PRICE_CURRENCY, DEFAULT_PRICE_CURRENCY),
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
