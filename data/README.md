# Data directory

Everything in ExecLens runs on **synthetic** data produced by its own seeded
generator. Nothing here is real market data, and every fill is simulated.

| Path | Tracked in git | Contents |
|---|---|---|
| `sample/` | yes (~0.8 MB) | Small demo dataset: 6 synthetic symbols, one 1-hour session, 240 parent orders, ~4.6k simulated fills. Regenerate with `execlens generate --preset sample --out data/sample`. |
| `raw/` | no | Place your own quote/trade/order/fill Parquet files here (same schema) to analyse them. |
| `generated/` | no | Datasets produced locally by `execlens generate` (for example the >1M-quote benchmark dataset). |
| `derived/` | no | Analysis outputs (order/fill TCA tables, diagnostics) written by `execlens analyze`. |

## Dataset layout

A dataset directory holds one Parquet file per table plus provenance metadata:

```
quotes.parquet    timestamp (UTC, us), symbol, bid_price, ask_price, bid_size, ask_size
trades.parquet    timestamp (UTC, us), symbol, price, size
orders.parquet    order_id, symbol, side (BUY/SELL), quantity, arrival_timestamp,
                  execution_horizon_seconds (nullable), strategy (nullable)
fills.parquet     fill_id, order_id, timestamp (UTC, us), quantity, price
metadata.json     generator version, full generation spec, seed, scenario parameters, disclaimer
```

See `docs/DATA_MODEL.md` for the validation rules and `docs/METRICS.md` for
how the tables are analysed.

## Synthetic symbols in the sample

| Symbol | Scenario |
|---|---|
| `SYN_NORMAL` | liquid, stable |
| `SYN_HIVOL` | high volatility |
| `SYN_ILLIQ` | low liquidity (sparse quotes and trades) |
| `SYN_WIDE` | wide spread |
| `SYN_UP` | upward trend |
| `SYN_DOWN` | downward trend |

The `SYN_` prefix is deliberate: these are not real tickers.
