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

### Integration

| Name     |  Type  | Required | Default | Description          |
|----------|:------:|:--------:|:-------:|----------------------|
| username | string |   yes    |         | ESO username / email |
| password | string |   yes    |         | ESO password         |
| objects  |  list  |   yes    |         | List of objects      |

### Object

| Name         |  Type   | Required | Default | Description                                          |
|--------------|:-------:|:--------:|:-------:|------------------------------------------------------|
| name         | string  |   yes    |         | Name of object (will be visible in energy dashboard) |
| id           | string  |   yes    |         | Object ID (see below *How to get your object ID*)    |
| consumed     | boolean |    no    |  True   | Generate statistics for consumed energy              |
| returned     | boolean |    no    |  False  | Generate statistics for returned energy              |
| price_entity | string  |    no    |         | Name of an entity tracking electricity price         |


### Example
```yaml
eso:
  username: your_username
  password: your_password
  objects:
    - name: My House
      id: 123456
      returned: True
    - name: My Flat
      id: 654321      
```

### How to get your object ID

1. Login to your ESO account
2. Go to your [objects page](https://mano.eso.lt/objects)
3. Click on desired object
4. Look at address bar of your browser
5. `https://mano.eso.lt/objects/123456789` - 123456798 is your object ID

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

eso:
  username: your_username
  password: your_password
  objects:
    - name: My House
      id: 123456
      price_entity: sensor.nordpool_kwh_eur_ext
```

The `price_entity` parameter of the ESO object (above) is pointed to the Nord Pool price entity. This triggers creation
of an additional HA entity tracking energy costs.

To display the Cost information in the HA Energy dashboard, in the Energy configuration popup click the `Use an entity tracking
the total costs` option and select the entity called `My House (cost)`.


# TODO

 - [ ]  Test with multiple objects
 
