# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.4.2] – 2026-11-18

### Added
- Billed kWh per meter read from each settlement (`kwh`, keyed by tariff
  slug, and `kwh_gesamt`), so consumption is available for every billing
  year, also before the portal's meter data starts.

### Fixed
- Text runs containing a "]" byte inside a TJ array were skipped.

## [0.4.1] – 2026-11-18

### Fixed
- Invoice reader: the text mapping is now chosen per font, so amounts in
  older invoices are no longer misread.
- The settlement total is the invoice's overall total ("Ihre Gesamtkosten"),
  which includes fees besides electricity.

### Added
- Per settlement: electricity cost (net, VAT, gross), other charges, state
  subsidy and the effective cost after the subsidy (`zuschuss`, `kosten`,
  `strom_netto`, `strom_ust`, `strom_brutto`, `sonstiges`).

## [0.4.0] – 2026-11-18

### Added
- Settlement totals read from the saved invoice PDFs (net, VAT, gross,
  paid part payments, balance and billing period). Dependency-free reader
  using the PDFs' own text mapping; each file is parsed once and nothing
  is stored besides the numbers.
- `sensor.burgenland_energie_abrechnung_gesamtkosten`: gross cost of the
  latest settlement, all settlements in the `abrechnungen` attribute.
- `gesamtbetrag` per entry in the invoice list; `rechnungen_ausgewertet` in
  the data status sensor.

## [0.3.0] – 2026-11-18

### Added
- `sensor.burgenland_energie_datenstand` (complete / incomplete / error):
  checks that every invoice is saved as a verified PDF, reports missing
  invoices, stale usage data and fetch errors.
- Signed per-invoice PDF links in the invoice list (valid 24 h, one file
  each, renewed on every update), served by an authenticated view.
- `download_invoice` with `all_missing: true` saves every missing invoice.
- `kwh_pro_monat` on the meter sensors.

### Fixed
- Invoice amounts are labelled correctly: the portal reports the amount to
  pay ("Zu zahlender Betrag"), not the invoice total (attribute
  `zahlbetrag`, sensor "Latest invoice · amount due").
- Yearly totals and the latest metered day now come from the stored
  statistics (whole history, kept across restarts) instead of the last
  import window.
- Up to 12 invoices are archived per update (was 3).

## [0.2.2] – 2026-11-18

### Fixed
- Handover dialog: explains to open the link with Ctrl/Cmd+click, because a
  plain click is handled by the Home Assistant frontend and closes the dialog.

## [0.2.1] – 2026-11-18

### Fixed
- The handover dialog shows the handover link itself, for browsers that
  block the automatically opened tab as a pop-up.

## [0.2.0] – 2026-11-18

### Added
- One-click token handover in setup and re-authentication: sign in to the
  portal, click a bookmark, done. The token travels in the URL fragment to a
  single-use, 15-minute Home Assistant page; pasting remains available.

## [0.1.0] – 2026-11-18

### Added
- Setup by handing over the portal's refresh token (the portal login is
  captcha-protected); renewed tokens are stored automatically, re-authentication
  when the portal rejects the token.
- Tariff sensors per meter: energy price gross and net, base price per month.
- Part-payment plan: next instalment and due date, calendar of all due dates.
- Latest invoice (amount, date, billing period, list of recent invoices).
- Bonus points.
- Billed daily energy per meter as long-term statistics
  (`burgenland_energie:<zaehler>_bezug`), re-imported daily.
- Actions `burgenland_energie.download_invoice` and
  `burgenland_energie.import_meter_data`; option to save new invoices as PDF
  in the media folder (owner-only file permissions).

### Security
- Token never logged; errors and logs omit contract, meter and customer
  numbers.
- Identifiers from the API are validated before use in URLs and file names.
- Own HTTP session without cookies; actions are admin-only; invoice lists are
  excluded from the recorder.
- Repository tooling: pre-commit hooks (gitleaks, personal-data check),
  CI with tests and validation, documented synthetic test-data policy.
