# Data model

ExecLens works on four tables, defined in `src/execlens/data/schemas.py`:

* **Polars column schemas** (`QUOTE_SCHEMA`, `TRADE_SCHEMA`, `ORDER_SCHEMA`,
  `FILL_SCHEMA`) for all bulk processing, enforced by the frame validators in
  `data/validation.py`;
* **Pydantic models** for the two records that cross a boundary one at a
  time: `Order` (a parent order handed to the simulator,
  `ParentOrder.from_row`) and `Fill` (fills returned by the API).

`tests/unit/test_validation.py` checks that the frame validators and the
Pydantic models reject the same invalid orders and fills.

## Conventions

| Topic | Rule |
|---|---|
| Time | Timezone-aware **UTC**, microsecond precision (`Datetime("us", "UTC")`). Naive timestamps are rejected; other time zones are converted to UTC (lossless). |
| Quantities | Integer shares. Whole-number floats are accepted and cast; fractional quantities are rejected. |
| Side | `"BUY"` or `"SELL"` (case-sensitive). Internally `side_sign = +1 / -1`. |
| Prices | Positive, finite floats. |
| Identifiers | `order_id` and `fill_id` must be unique. |

## Tables

### `quotes` - top of book (Level 1)

| Column | Type | Rule |
|---|---|---|
| `timestamp` | datetime (UTC, us) | required |
| `symbol` | string | non-empty |
| `bid_price` | float | > 0, finite |
| `ask_price` | float | > 0, finite |
| `bid_size` | int | >= 0 |
| `ask_size` | int | >= 0 |
| - | - | `bid_price <= ask_price` (locked markets allowed, **crossed markets rejected**) |

Derived by the engine (never stored): `mid = (bid + ask) / 2`,
`spread = ask - bid`, `spread_bps = spread / mid * 10,000`.

Duplicate `(symbol, timestamp)` rows are allowed. The last one in input order
is the state at that instant.

### `trades` - the market tape

| Column | Type | Rule |
|---|---|---|
| `timestamp` | datetime (UTC, us) | required |
| `symbol` | string | non-empty |
| `price` | float | > 0, finite |
| `size` | int | > 0 |

### `orders` - parent orders

| Column | Type | Rule |
|---|---|---|
| `order_id` | string | unique |
| `symbol` | string | non-empty |
| `side` | string | `BUY` or `SELL` |
| `quantity` | int | > 0 |
| `arrival_timestamp` | datetime (UTC, us) | when the order reached the desk |
| `execution_horizon_seconds` | float, nullable | > 0 when present |
| `strategy` | string, nullable | a label such as `TWAP`; the simulator uses it to pick the algorithm |

### `fills` - executions

| Column | Type | Rule |
|---|---|---|
| `fill_id` | string | unique |
| `order_id` | string | must exist in `orders` |
| `timestamp` | datetime (UTC, us) | not before the order's `arrival_timestamp` |
| `quantity` | int | > 0 |
| `price` | float | > 0, finite |
| - | - | per order, `sum(quantity) <= orders.quantity`: **overfills are rejected, never silently accepted** |

## Validation behaviour

`src/execlens/data/validation.py` evaluates every rule as a Polars expression
over the whole table. It collects **all** violations, across all four tables,
into one `DataValidationError` that lists each rule, the number of affected
rows and up to five examples. For example:

```
2 data validation issue(s):
  - [quotes] crossed market: bid_price > ask_price (3 affected); e.g. row 17 (SYN @ 2026-01-05 14:31:02 UTC), ...
  - [fills] total filled quantity exceeds the parent order quantity (1 affected); e.g. O1 (filled 101 > ordered 100)
```

Invalid data is rejected, never repaired. The only normalisation is lossless:
time zone conversion to UTC, microsecond precision, widening integer casts and
canonical column order. `read_dataset()` re-validates every table when it
loads from disk.

## On disk

A dataset directory contains one zstd-compressed Parquet file per table and a
`metadata.json` provenance record: the generator version, the full generation
spec including the seed, the scenario parameters and a synthetic-data
disclaimer. `execlens analyze` writes derived tables (`orders_tca.parquet`,
`fills_tca.parquet`, `flag_counts.parquet`, `summary.json`) to a separate
directory (default `data/derived/<dataset>`). `execlens query` exposes both
sets to SQL through DuckDB views.

## Analysis outputs (derived)

| Table | Grain | Main columns |
|---|---|---|
| fill-level TCA | one row per fill | quote context (`fill_quote_status`, `fill_mid`, `fill_spread_bps`), `effective_spread_bps`, `future_mid_{h}s`, `markout_{h}s_status`, `markout_{h}s_bps`, `mid_markout_{h}s_bps` for h in 10/30/60/300 s |
| order-level TCA | one row per order | arrival context, execution statistics, benchmark interval, `market_volume`, `market_vwap`, arrival/VWAP cost, effective spread, markouts with unavailable-fill counts, both participation measures, diagnostic features, `flags` / `flag_reasons` / `context_flags` / `context_notes` / `is_flagged` |

The definitions are in [METRICS.md](METRICS.md).
