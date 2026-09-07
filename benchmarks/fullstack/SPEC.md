# Stockroom — a real coding challenge

Build the app yourself, not a plan or a mockup. The current directory is a disposable app workspace. An existing SQLite database is at `data/inventory.sqlite`. It contains synthetic data only. Inspect it through the warehouse MCP tools; do not assume its schema or replace its data. No source app has been provided.

## First action: genuine multi-call

In your first assistant response, emit BOTH `mcp__warehouse__db_schema` and `mcp__warehouse__db_profile` as separate tool calls in ONE response. They are independent. Do not substitute one Bash command or two sequential assistant turns. Then use `db_query` to inspect representative rows and constraints. Batch other independent calls when appropriate; never batch a build before its source files are written.

## Build and runtime contract

- Use Python 3 standard library + SQLite for the backend, plain TypeScript for the frontend, and the installed Bun bundler. No pip/npm installs, external services, CDNs, or real credentials.
- `python3 build.py` must actually run `bun build` on `web/app.ts`, create a bundled `dist/app.js`, and copy your HTML/CSS into `dist`. Do not just write a fake build log.
- `python3 app.py --db PATH --port PORT` must start the HTTP app on **127.0.0.1 only**. Start it from Bash with stdout/stderr redirected to `app.log`, save its PID in `app.pid`, and exercise it over HTTP.
- Migrate the supplied DB idempotently on startup, without deleting/reseeding existing data or changing existing table semantics. Add a table for transfer idempotency as needed. Preserve unknown tables.
- Write meaningful tests in `tests/`; run them with `python3 -m unittest discover -s tests -v`. Also run the independent verifier at the path given in the task prompt. Do not modify the verifier, its seed generator, MCP server, or this spec. Fix YOUR app when checks fail.
- Leave a concise `README.md` explaining build, run, tests, database migrations, and limitations. Do not put keys in any file.

## API contract

Return JSON for API responses/errors. Errors must have an `error` string. No tracebacks or SQL internals in responses.

### GET /api/health

Return `{"ok": true}` after the database is ready.

### GET /api/warehouses

Return an array of `{id, name}` objects sorted by id.

### GET /api/summary

Return `{product_count, available_units, inventory_value_cents, low_stock_count}` for **active products only**:

- product_count: number of active products, not stock rows.
- available_units: sum of on_hand minus reserved across warehouses.
- inventory_value_cents: sum of on_hand times price_cents, as an integer.
- low_stock_count: number of stock rows where available is less than that product's reorder_point.

### GET /api/inventory

Optional query parameters: `warehouse_id`, `q`, `limit` (default 20, range 1–100), and `offset` (default 0, nonnegative).

Return `{"total": N, "items": [...]}`. Each item contains `product_id, sku, name, warehouse_id, warehouse_name, on_hand, reserved, available, price_cents, reorder_point`. Only active products; sort by sku then warehouse_id. `total` counts all filtered rows before pagination.

`q` is a literal, case-insensitive substring of sku or name; Unicode and literal percent/underscore must work. Parameterize SQL: an SQL-looking search string must not alter the query. Invalid numeric parameters return HTTP 400.

### POST /api/transfers

Input: `{product_id, from_warehouse_id, to_warehouse_id, quantity, idempotency_key}`.

- IDs and quantity must be positive JSON integers, not booleans, fractions or numeric strings. Idempotency key is a nonempty string, maximum 128 characters. Reject invalid input with 400, missing entities with 404, insufficient available stock with 409, and transfers within one warehouse with 400.
- Successful transfers return HTTP 200, `ok: true`, plus useful transfer details.
- Atomically decrease source on_hand, increase destination on_hand (creating its row if necessary), and write exactly two stock_movements rows with opposite deltas. Reserved quantities stay unchanged; total stock is conserved. Use a transaction that is safe under concurrent requests.
- Replay of the same key and identical payload returns the identical successful response without any stock/movement change. Reusing a key with a different payload returns 409. Concurrent duplicate requests must not double-apply.
- Two concurrent transfers cannot both spend the same stock. Losing requests must return 409 rather than 500, and must leave no partial stock/audit updates.

## User interface

A functioning dark-only inventory dashboard: clear header, four summary cards, a searchable/filterable inventory table, pagination, and a transfer form. The form uses the real API and refreshes stock/summary after success. Show useful inline validation, server errors, empty states and loading states. Render database strings as text, not raw HTML.

Use restrained dark surfaces (#191919/#202020), white primary text, muted secondary text, blue #5E9FE8 actions, 16px system type, 8px radii, and generous spacing. No decorative hero or images are needed. Give every control an accessible label, visible keyboard focus and a 44px target. At 390px, collapse columns and keep table scrolling inside its own region—not across the whole page. Do not expose the database or arbitrary filesystem paths through the static server.

## Completion

Actually build, start the server, run your tests and the independent tests, fix failures, and verify a real transfer through HTTP. Keep output concise to conserve the API credit. Finish with `FULLSTACK_DONE` only after your commands pass; report any remaining failures honestly. Work only in this workspace. Do not modify emutools or any evaluator files.
