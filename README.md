# Your support
This open-source project is developed in my free time.
Your donation would help me dedicate more time and resources to improve project, add new features, fix bugs,
as well as improve motivation and helps me understand, that this project is useful not only for me, but for more users.

<a href="https://www.buymeacoffee.com/algirdasci" target="_blank"><img src="https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png" alt="Buy Me A Coffee" style="height: 41px !important;width: 174px !important;box-shadow: 0px 3px 2px 0px rgba(190, 190, 190, 0.5) !important;-webkit-box-shadow: 0px 3px 2px 0px rgba(190, 190, 190, 0.5) !important;" ></a>

# Intro
This integration is for users which have smart [ESO](https://mano.eso.lt/) energy meters and does not have
technical possibilities to add P1 interface (for example meter is far away from wireless reception).
Keep in mind, that ESO site provides data for last 24 hours,
therefore refresh rate is quite slow (check for new data is performed every 2 hours).
If you wish for real-time statistics - consider using 3rd party meters (like Shelly 3EM) or utilise P1 interface of smart meter.
The daily ESO import is scheduled once per day at a random time between 05:10 and 07:10 to avoid all installations calling ESO at the same moment.

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
3. **Step 1 – ESO account:** enter your [mano.eso.lt](https://mano.eso.lt/) username and password.
4. **Step 2 – Two-factor authentication (email):** ESO emails a one-time code on every login, so a mailbox is **required**. Enter the mailbox that receives those codes so Home Assistant can read them automatically (see *Two-factor authentication* below).
5. **Step 3 – Select objects:** the integration logs in and **auto-discovers your eso objects**.

After setup, the account appears under **Settings → Devices & Services** with each object listed beneath it:

- **Add object** – discovers your objects and adds one as a new entry.
- **Reconfigure** (per object) – set that object's name, consumed/returned tracking, and an optional price entity for cost statistics — directly on the object, no nested menus.
- **Configure** (on the account) – update the ESO password and mailbox (2FA) settings.
- **Delete** (per object) – stop tracking that object.

#### Migrating from YAML

 If you already have an `eso:` block in `configuration.yaml`, it is imported automatically into a config entry on the next restart.
 Once the integration appears under **Settings → Devices & Services**, remove the `eso:` block from `configuration.yaml`.

### Two-factor authentication

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


# TODO

 - [ ]  Test with multiple objects

