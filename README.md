# Your support
This open-source project is developed in my free time.
Your donation would help me dedicate more time and resources to improve project, add new features, fix bugs,
as well as improve motivation and helps me understand, that this project is useful not only for me, but for more users.

<a href="https://www.buymeacoffee.com/algirdasci" target="_blank"><img src="https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png" alt="Buy Me A Coffee" style="height: 41px !important;width: 174px !important;box-shadow: 0px 3px 2px 0px rgba(190, 190, 190, 0.5) !important;-webkit-box-shadow: 0px 3px 2px 0px rgba(190, 190, 190, 0.5) !important;" ></a>

# Intro
This integration is for users which have smart energy meters and do not have
technical possibilities to add P1 interface (for example meter is far away from wireless reception).
It supports two data providers, selectable during setup:

- **[ESO](https://mano.eso.lt/)** – signs in with your mano.eso.lt account. ESO emails a one-time
  code on every login, so a mailbox is required to read it automatically. The daily import runs once
  per day at a random time between 05:10 and 07:10 to avoid all installations calling ESO at once.
- **Ignitis** – signs in with your **Ignitis "Energy Smart" app credentials** (the same email and
  password you use in the Energy Smart app). No mailbox/two-factor step is needed. The daily import
  runs at 10:30, once the previous day's data is published; if it isn't ready yet, it retries every
  10 minutes for a while.

Keep in mind that providers publish data for the previous day only, so the refresh rate is slow.
If you wish for real-time statistics - consider using 3rd party meters (like Shelly 3EM) or utilise P1 interface of smart meter.

### Disclaimer

**This component is in testing stage! Errors or miscalculation, breaking changes should be expected! Any feedback or requests should be raised as an [issue](https://github.com/algirdasc/hass-eso/issues)**.

# Installation

### HACS
1. Navigate to HACS Integrations
2. Click `Custom repositories`
3. Paste repository URL `https://github.com/algirdasc/hass-eso` to `Repository` field
4. Choose `Integration` category
5. Click `Add`
6. Install & configure component (see Configuration)
7. Restart HA

### Native

1. Upload `custom_components` directory to your HA `config` directory
2. Configure component (see Configuration)
3. Restart HA

# Configuration

The integration is configured entirely from the Home Assistant UI (config flow).
Legacy YAML configuration is **deprecated**; any existing `eso:` block is automatically imported into the UI on startup (see *Migrating from YAML*).

### UI configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **ESO Energy Consumption**
3. **Step 1 – Account:** choose your **data provider** (ESO or Ignitis) and enter your credentials.
   - **ESO:** your [mano.eso.lt](https://mano.eso.lt/) username and password.
   - **Ignitis:** your **Ignitis "Energy Smart"** app email and password.
4. **Step 2 – Ignitis login method:** *Ignitis only.* Choose how Home Assistant signs in (see *Ignitis login methods* below), and confirm your password. ESO skips this step.
5. **Step 2 – Two-factor authentication (email):** *ESO only.* ESO emails a one-time code on every login, so a mailbox is **required**. Enter the mailbox that receives those codes so Home Assistant can read them automatically (see *Two-factor authentication* below). Ignitis skips this step.
6. **Step 3 – Select objects:** the integration logs in and **auto-discovers your objects**.

After setup, the account appears under **Settings → Devices & Services** with each object listed beneath it:

- **Add object** – discovers your objects and adds one as a new entry.
- **Reconfigure** (per object) – set that object's name, consumed/returned tracking, and cost/balance options — directly on the object, no nested menus.
- **Configure** (on the account) – update the account password (for ESO, the mailbox/2FA settings; for Ignitis, the login method).
- **Delete** (per object) – stop tracking that object.

#### Ignitis login methods

Ignitis accounts can sign in two ways. Both end up with the same API token, so the data and the
sensors are identical — only the way the token is obtained differs.

| Method | What it does |
| ------ | ------------ |
| **Direct** (default) | Posts your email and password straight to the Energy Smart API. One request. |
| **Self sign portal** | Reproduces the mobile app's flow: mints a login ticket, signs in through `e.ignitis.lt`, then trades the ticket for an API token. Four requests. |

Start with **Direct**. If it reports invalid credentials even though they are correct, switch to
**Self sign portal** — some accounts are only accepted through the web login. You can change the method
later under **Configure** on the account.

#### Migrating from YAML

 If you already have an `eso:` block in `configuration.yaml`, it is imported automatically into a config entry on the next restart.
 Once the integration appears under **Settings → Devices & Services**, remove the `eso:` block from `configuration.yaml`.

### Two-factor authentication (ESO only)

This applies to the **ESO** provider only; Ignitis signs in directly and needs no mailbox.

ESO now sends a mandatory one-time code by email on **every** login. When a mailbox
is configured (UI step 2), the integration completes that step automatically: it reads
the latest code from your mailbox and submits it. To keep email traffic to a minimum
it persists the authenticated session (`eso_session.json` in the HA config directory,
valid ~3 weeks) and only performs a full login + 2FA when that session has expired.

A mailbox is **required** — without it the integration cannot log in while 2FA is
enforced on your account.

The mailbox settings you provide are:

| Name     |  Type  | Required |    Default     | Description                                                                 |
|----------|:------:|:--------:|:--------------:|-----------------------------------------------------------------------------|
| host     | string |   yes    | imap.gmail.com | IMAP server host                                                            |
| port     |  int   |   yes    |      993       | IMAP server port (SSL)                                                      |
| username | string |   yes    |                | Mailbox username                                                            |
| password | string |   yes    |                | Mailbox password. For Gmail this must be an [app password](https://support.google.com/accounts/answer/185833), not the account password |
| sender   | string |    no    | savitarna@eso.lt | Sender address the 2FA code is matched on                                 |
| folder   | string |    no    |     INBOX      | Mailbox folder to search                                                    |

### Object settings

Each object (metering point) exposes the following settings via **Reconfigure**:

| Name           |  Type   | Required | Default | Description                                          |
|----------------|:-------:|:--------:|:-------:|------------------------------------------------------|
| name           | string  |   yes    |         | Name of object (will be visible in energy dashboard) |
| id             | string  |   yes    |         | Object ID (auto-discovered during setup)             |
| consumed       | boolean |    no    |  True   | Generate statistics for consumed energy              |
| returned       | boolean |    no    |  False  | Generate statistics for returned energy              |
| price_entity   | string  |    no    |         | Name of an entity tracking electricity price         |
| price_currency | string  |    no    |   EUR   | Currency of electricity price                        |
| fixed_price    |  float  |    no    |         | Flat price per kWh, used for cost when no price entity is set |
| export_balance | boolean |    no    |  False  | Track the accumulated export balance reported by Ignitis     |

### Stored energy (storage bank) sensor

For objects with **returned** enabled, the integration creates a
`sensor.eso_stored_energy_<object_id>` entity with the official storage-bank
("pasaugojimas") balance from the self-service portal — the same number the
portal shows under *Rodyti sukauptos energijos kiekį*.

The sensor reports the balance (kWh) at the end of the last **closed** month of
the current bank year (April 1 – March 31); ESO reports the still-open month as
0 until it closes it. The full monthly series is available in the `series`
attribute. Note that the bank never goes below zero: a deficit month (imported
more than exported) drains it at most to 0, so a running `returned − consumed`
sum can understate the real balance. The sensor refreshes together with the
daily import and keeps its last value across restarts.

### Example with cost calculation

The example below is using the [Nord Pool integration for Home Assistant](https://github.com/custom-components/nordpool).
It creates an entity tracking spot market (hourly) electricity price. The `additional_costs` parameter is
used to add any cost margins which depend on a particular energy contract.

```yaml
sensor:
  - platform: nordpool
    region: "LT"
    currency: "EUR"
    VAT: true
    precision: 5
    low_price_cutoff: 0.95
    price_in_cents: false
    price_type: kWh
    additional_costs: "{{ 0.08470 + 0.007 | float }}" # 0.08470 ESO, 0.007 ENEFIT
```

Then, when reconfiguring an object, set its **price entity** to the Nord Pool price
entity (e.g. `sensor.nordpool_kwh_eur_ext`). This triggers creation of an additional
HA entity tracking energy costs.

To display the Cost information in the HA Energy dashboard, in the Energy configuration popup click the `Use an entity tracking
the total costs` option and select the entity called `My House (cost)`.

If you have a flat tariff instead of an hourly price sensor, leave **price entity** empty and set a
**fixed price** per kWh on the object; the cost statistics are then calculated from that flat rate.

### On-demand import

The `eso.import_now` service triggers an import immediately instead of waiting for the daily run.
All fields are optional:

| Field             | Description                                                                                     |
|-------------------|-------------------------------------------------------------------------------------------------|
| `config_entry_id` | The account(s) to import. Leave empty to import every configured account.                        |
| `date_from`       | First day to import. Set it to backfill history instead of importing the usual daily window.      |
| `date_to`         | Last day of the backfill range (defaults to today). Requires `date_from`.                         |

Without `date_from` the service imports the same window as the daily run. With it, the whole
`date_from`…`date_to` range is fetched hourly — useful for filling gaps or seeding history on a new
install. Backfill works for **both providers**; long ranges are split into several requests
automatically (ESO in ~90-day chunks, Ignitis in weekly chunks, because the Ignitis API serves at
most 8 days per request and silently truncates anything wider). Since Ignitis publishes complete days
only, an Ignitis range is clamped to yesterday. Expect a long Ignitis backfill to take a while — a
request per week of history, one second apart (verified: 180 days imports in ~26 requests).

A backfill imports the energy (and cost) series only. The **export balance** is skipped, because
providers report just the balance as it stands today and no history for it — recording it during a
backfill would file today's balance under a past hour.

Backfill imports are one-offs: unlike the daily run they are not retried automatically if a fetch
fails. A backfill that ends up short logs an error naming the days it is missing, so re-run the
service for those.

One caveat on ordering: Home Assistant stores a running total per hour, and an import continues the
total from the last hour before the range it writes. Statistics that already exist *after* the
imported range keep their old totals, so filling a gap that sits before data you already have leaves
a seam at the join — usually a one-off spike or a flat hour in the energy dashboard. To avoid it,
end the range at today (`date_from` only) so everything after the gap is rewritten too.

```yaml
action: eso.import_now
data:
  date_from: "2026-01-01"
  date_to: "2026-06-30"
```


# TODO

 - [ ]  Test with multiple objects

