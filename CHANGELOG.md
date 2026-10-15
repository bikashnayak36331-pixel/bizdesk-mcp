# Changelog

## 1.0.0 (2026-10-15)

First public release.

- 21 MCP tools covering the business profile, clients, quotes, invoices, payments, reports, reminder drafts and PDF export.
- Resources: `bizdesk://profile`, `bizdesk://reports/overdue`, `bizdesk://documents/{number}`.
- Prompts: `month_end_review`, `quote_new_job`.
- SQLite storage with transactions, an audit log and exact money maths (integer cents, half-up rounding).
- Read-only mode, stdio and Streamable HTTP transports.
- Demo data (`--seed-demo`) and a recorded session in `docs/`.
- 49 tests: unit, PDF content, in-process MCP client, stdio subprocess and HTTP transport.
