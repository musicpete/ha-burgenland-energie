# Burgenland Energie for Home Assistant

[![CI](https://github.com/musicpete/ha-burgenland-energie/actions/workflows/ci.yml/badge.svg)](https://github.com/musicpete/ha-burgenland-energie/actions/workflows/ci.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
![Version](https://img.shields.io/badge/version-0.1.0-blue)
![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.11%2B-03A9F4)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Unofficial Home Assistant integration for the **Burgenland Energie customer
portal** ([meine.burgenlandenergie.at](https://meine.burgenlandenergie.at)).
It brings your tariff, part-payment plan, invoices, bonus points and billed
energy per meter into Home Assistant.

> **Not affiliated** with Burgenland Energie. The portal has no public API;
> this integration uses the same requests as the portal web app. A portal
> update can break it until the integration is updated.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Entities and statistics](#entities-and-statistics)
- [Actions](#actions)
- [Dashboard examples](#dashboard-examples)
- [Privacy and security](#privacy-and-security)
- [Troubleshooting](#troubleshooting)
- [How it works](#how-it-works)
- [Development](#development)
- [Changelog](CHANGELOG.md) · [License](LICENSE)

## Features

| Area | What you get |
| --- | --- |
| Tariff per meter | Energy price gross and net (EUR/kWh), base price per month; usable as the price in the Energy dashboard |
| Part payments | Next instalment and its due date, remaining instalments, a calendar with all due dates |
| Invoices | Latest invoice and settlement with billing period; optional automatic PDF archive in the media folder |
| Bonus points | Current balance of the Bonus- und Servicewelt |
| Energy | Billed energy per meter and day as long-term statistics, with full history on demand |

## Requirements

- Home Assistant **2025.11** or newer
- HACS (up to HA 2026.10) or the built-in Marketplace (HA 2026.11+), or file access for a manual install
- An account for meine.burgenlandenergie.at

## Installation

### HACS

1. HACS → **⋮** → **Custom repositories**
2. Repository `https://github.com/musicpete/ha-burgenland-energie`, type **Integration** → **Add**
3. Search **Burgenland Energie (Kundenportal)** → **Download**
4. Restart Home Assistant

### Marketplace (HA 2026.11+)

Settings → Marketplace → **Add from link** →
`https://github.com/musicpete/ha-burgenland-energie` (category *Integration*) → install → restart.

### Manual

Copy `custom_components/burgenland_energie` to
`/config/custom_components/burgenland_energie` and restart.

## Configuration

The portal login is protected by a captcha, so Home Assistant cannot sign in
with your password. Instead it takes over an existing portal session once,
via the session's **refresh token**, and renews it from then on by itself.

1. Sign in at [meine.burgenlandenergie.at](https://meine.burgenlandenergie.at)
   and tick **Angemeldet bleiben**.
2. Copy the token, either
   - with a bookmark whose address is
     ```
     javascript:navigator.clipboard.writeText(localStorage.getItem('okc-auth-refreshToken')||'').then(()=>alert('Token kopiert'))
     ```
     clicked while the portal page is open, or
   - in the browser's developer tools (F12) → Console:
     `copy(localStorage.getItem('okc-auth-refreshToken'))`
3. Settings → Devices & services → **Add integration** →
   **Burgenland Energie (Kundenportal)** → paste the token.

Afterwards you can close the browser, but **do not sign out of the portal**
(*Abmelden*): signing out revokes the token, and Home Assistant asks for a new
one. When that happens a repair notification appears; repeat steps 1–2.

**Options** (Configure on the integration):

| Option | Default | Description |
| --- | --- | --- |
| Save invoices as PDF | off | New invoices are stored in `<media>/burgenland_energie/` (up to 3 per poll) |
| Days of usage data per run | 45 | Days up to yesterday re-read on every daily import |

## Entities and statistics

`<tarif>` is derived from the tariff name, e.g. `tarif_a`,
`tarif_b_ae`; `<zaehler>` is `zp_` plus the last six digits of the meter's
point of delivery (Zählpunkt).

| ID | Type | Description |
| --- | --- | --- |
| `sensor.burgenland_energie_naechste_teilzahlung` | EUR | Next instalment; attributes `faellig_am`, `status`, `verbleibende_teilzahlungen`, `summe_verbleibend`, `plan` |
| `sensor.burgenland_energie_naechste_teilzahlung_am` | date | Due date of the next instalment |
| `calendar.burgenland_energie_teilzahlungen` | calendar | All instalment due dates |
| `sensor.burgenland_energie_letzte_rechnung` | EUR | Latest invoice; attributes `rechnungsnummer`, `datum`, `zeitraum_von`, `zeitraum_bis`, `jahresabrechnung`, `rechnungen` |
| `sensor.burgenland_energie_letzte_rechnung_am` | date | End of the billing period (settlements) or invoice date |
| `sensor.burgenland_energie_bonuspunkte` | points | Bonus points; attribute `faktor` |
| `sensor.burgenland_energie_<tarif>_arbeitspreis` | EUR/kWh | Energy price incl. 20 % VAT; attributes `netto`, `tarif`, `preisgarantie_bis`, `bindung_bis` |
| `sensor.burgenland_energie_<tarif>_arbeitspreis_netto` | EUR/kWh | Energy price excl. VAT |
| `sensor.burgenland_energie_<tarif>_grundpreis` | EUR | Base price per month incl. VAT; attributes `netto_pro_monat`, `brutto_pro_jahr` |
| `sensor.burgenland_energie_<tarif>_letzter_messtag` | date | Latest day with billed energy; attributes `kwh`, `kwh_pro_jahr`, `fehler` |
| `burgenland_energie:<zaehler>_bezug` | statistic (kWh) | Billed energy per day |

Prices are energy prices only. Grid charges (Netz Burgenland), taxes and
levies are billed on top and are not part of these sensors.

**About the energy statistic:** it contains what Burgenland Energie bills
you, i.e. grid energy. Members of an energy community get the community share
from the community, which is not included here. Values arrive a few days
late; each daily run (08:43) re-reads the configured window so corrections
replace earlier values.

## Actions

### `burgenland_energie.download_invoice`

Saves an invoice PDF to `<media>/burgenland_energie/` and returns its path.

| Field | Default | Description |
| --- | --- | --- |
| `bill_nr` | latest | Invoice number (see attribute `rechnungen`) |

### `burgenland_energie.import_meter_data`

Re-reads the billed daily values of all meters.

| Field | Default | Description |
| --- | --- | --- |
| `days` | 45 | Days up to yesterday; e.g. `1500` loads the full history |

## Dashboard examples

Upcoming instalments:

```yaml
type: calendar
initial_view: listWeek
entities:
  - calendar.burgenland_energie_teilzahlungen
```

Billed energy per month:

```yaml
type: statistics-graph
title: Billed energy · kWh per month
chart_type: bar
period: month
days_to_show: 730
stat_types: [change]
entities:
  - entity: burgenland_energie:zp_000001_bezug
    name: Meter A
```

## Privacy and security

**What is stored in Home Assistant**

| Data | Where | Why |
| --- | --- | --- |
| Portal refresh token | Config entry (`.storage/core.config_entries`, plain text like every HA integration credential) | Renewing the session; replaced automatically when the portal issues a new one |
| Customer number | Config entry (id and data) | Identifies the account; needed for API requests |
| Tariffs, instalments, latest invoice, bonus points | Entity states and attributes (recorder) | The sensors |
| Billed kWh per meter and day | Recorder long-term statistics | Usage history |
| Invoice PDFs (only if enabled) | `<media>/burgenland_energie/`, file mode 600 | Your archive |

Invoice numbers and the invoice/instalment lists are attributes that are
**not** written to the recorder database.

**What is sent where**

- Only to `api.meine.burgenlandenergie.at` over HTTPS with certificate
  verification, using a client session without cookies.
- The token is never written to logs or error messages.

**What is read and never stored**

Contract data also contains your address, e-billing address and bank details.
The parser reads only contract, meter and invoice numbers, tariffs, amounts
and dates; everything else is discarded right away. Identifiers taken from
the API are accepted only as plain alphanumerics before they are used in a
URL or file name.

**Logs and errors** name the endpoint without contract, meter or customer
numbers (e.g. `/api/contracts/…/bills`) and contain HTTP status codes only.

**Permissions:** both actions are admin-only. Saved invoices can be opened by
every Home Assistant user who can use the media browser, so keep the option
off if that is not acceptable.

**The token:** it grants access to your portal account like a password.
Copy it only from the official portal page, paste it only into Home
Assistant, and clear the clipboard afterwards. Signing out of the portal
revokes it.

Report security issues as described in [SECURITY.md](SECURITY.md).

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| "The portal rejected the token" | Sign in to the portal again and copy the current token; make sure the whole text was pasted |
| Repair notification "Re-authentication required" | The token was revoked (e.g. by *Abmelden*); copy a new one |
| `letzter_messtag` lags behind | Normal: billed values arrive a few days late |
| Invoices are not saved | Check that a media folder exists and the log for `custom_components.burgenland_energie` |

## How it works

- **Auth:** `POST /papi/auth/signin/refresh` with the refresh token returns a
  one-hour id token (AWS Cognito). API calls send it as raw `Authorization`
  header. A renewed refresh token returned by the portal is stored.
- **Data:** `/api/contracts` (tariffs per point of delivery),
  `/api/contracts/{nr}/bills` and `…/bills/{billNr}` (PDF),
  `/api/contracts/{nr}/part-payment-plan`, `/api/profile/bonus-points`,
  `/api/contracts/{nr}/meter-data/{pod}/v2` (daily kWh, yearly chunks).
- Polling every 6 hours; usage import once a day.
- All requests and parsing live in `api.py`.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the test data policy, the
pre-commit hooks (gitleaks secret scan and a personal-data check) and how to
run the tests. CI runs the same checks, the tests and Home Assistant/HACS
validation on every push.
