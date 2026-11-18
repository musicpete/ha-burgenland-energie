# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
