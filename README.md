# Nanoleaf Panels for Home Assistant

Control individual Nanoleaf panels from Home Assistant automations with support for:

- Per-panel solid colors
- Per-panel manual multi-frame animations
- Built-in animation presets
- Per-panel conditional animation with fallback colors
- Safe merge behavior (unspecified panels keep their current state)

This custom integration extends the official Home Assistant Nanoleaf integration and reuses its existing device authentication.

## Features

- Works with panel number indexing (1..N) or direct panel IDs
- Supports RGB and HSB color input
- Supports controller-side animation loops using Nanoleaf custom effects
- Built-in animation presets:
  - breathe: smooth bright -> dim -> off -> dim loop
  - pulse: on, then short off loop
  - strobe: quick double flash loop
  - cycle: full -> medium -> low brightness loop
  - fade: off -> low -> medium -> full loop
- Supports condition templates per panel:
  - If condition is true, run animation preset
  - If condition is false, apply fallback color

## Requirements

- Home Assistant with the official Nanoleaf integration configured
- A Nanoleaf Shapes/Canvas/Elements style panel controller supported by the official integration

## Installation (HACS)

[![Open your Home Assistant instance and add this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=UnsignedLong&repository=hacs-nanoleaf-panels&category=integration)

1. Open HACS in Home Assistant.
2. Go to Integrations.
3. Open the 3-dot menu, select Custom repositories.
4. Add your GitHub repository URL as category Integration.
5. Search for Nanoleaf Panels and install.
6. Restart Home Assistant.
7. Go to Settings -> Devices & Services -> Add Integration.
8. Search for Nanoleaf Panels and finish setup in the UI.

## Service

Service name:

- nanoleaf_panels.set_panels

### Service data

- entity_id: target Nanoleaf light entity from official integration
- panels: list of panel updates

Each panel item can use one of these forms:

1) Solid color

```yaml
- number: 1
  r: 255
  g: 120
  b: 0
```

or

```yaml
- number: 1
  hue: 30
  saturation: 100
  brightness: 100
```

2) Manual frames

```yaml
- number: 2
  frames:
    - r: 255
      g: 0
      b: 0
      transition_time: 5
    - r: 0
      g: 0
      b: 255
      transition_time: 5
```

3) Preset animation

```yaml
- number: 3
  animation: breathe
  color:
    r: 255
    g: 150
    b: 0
  speed: slow
```

4) Conditional preset animation with fallback

```yaml
- number: 4
  animation: pulse
  color:
    r: 255
    g: 80
    b: 0
  speed: medium
  fallback:
    r: 255
    g: 228
    b: 196
  condition: "{{ is_state('binary_sensor.motion', 'on') }}"
```

## Example: Mixed status + conditional animations

This example shows a single automation that sets all panels at once. Static panels
use fixed colors, while conditional panels animate when their sensor is active and
fall back to warm white otherwise. Unspecified panels keep their current state.

```yaml
alias: Nanoleaf Panel Status
triggers:
  - trigger: state
    entity_id:
      - binary_sensor.motion_sensor
      - binary_sensor.door_sensor
      - sensor.battery_level
      - light.nanoleaf_shapes
conditions:
  - condition: state
    entity_id: light.nanoleaf_shapes
    state: "on"
actions:
  - action: nanoleaf_panels.set_panels
    data:
      entity_id: light.nanoleaf_shapes
      panels:
        # Panel 1: battery level — green when full, red when low
        - number: 1
          r: "{{ [255, (2 * (100 - states('sensor.battery_level') | int)) | round | int] | min }}"
          g: "{{ [200, (2 * states('sensor.battery_level') | int) | round | int] | min }}"
          b: 0
        # Panel 2: warm white static
        - number: 2
          r: 255
          g: 228
          b: 196
        # Panel 3: breathe amber when door sensor active, else warm white
        - number: 3
          animation: breathe
          color:
            r: 255
            g: 150
            b: 0
          speed: slow
          fallback:
            r: 255
            g: 228
            b: 196
          condition: "{{ is_state('binary_sensor.door_sensor', 'on') }}"
        # Panel 4: pulse orange-red when motion active, else warm white
        - number: 4
          animation: pulse
          color:
            r: 255
            g: 80
            b: 0
          speed: medium
          fallback:
            r: 255
            g: 228
            b: 196
          condition: "{{ is_state('binary_sensor.motion_sensor', 'on') }}"
mode: restart
```

## Notes

- transition_time is in units of 100 ms.
- Built-in preset animations are rendered as Nanoleaf controller-side custom loops.
- Unspecified panels are preserved using current static state, then in-memory cache fallback.

## Repository layout

```text
custom_components/
  nanoleaf_panels/
    __init__.py
    manifest.json
    services.yaml
```

## Development and releases

- Tag releases using semantic versioning, for example: v1.1.0
- Keep custom_components/nanoleaf_panels/manifest.json version in sync with releases
- Create GitHub releases from tags so HACS users can install pinned versions

## Disclaimer

This project is community maintained and is not affiliated with Nanoleaf.
