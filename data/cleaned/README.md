# data/cleaned

| File | In git | Description |
|---|---|---|
| `transactions_clean.csv` | No (1.06 GB) | All 5,000,000 cleaned rows, 25 columns. Rebuild: `python scripts/01_clean_data.py` |
| `transactions_clean_sample_10k.csv` | Yes | Reproducible random sample (DuckDB reservoir, seed 42) used by the Excel workbook |

Column definitions: [`../../data_dictionary.md`](../../data_dictionary.md). Transformations: [`../../methodology.md`](../../methodology.md).
