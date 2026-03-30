# Logfire Trace Stats: GEPA Flash Lite Runs

Time window used for all queries: `2026-03-14T00:00:00Z` to `2026-03-27T23:00:00Z`  
Query: same aggregation query provided in chat, with `LIMIT 100` added for MCP safety.

## Run: GEPA Base gemini-3.1-flash-lite

Trace ID: `019d1f6f2eb61dc581d4a271d9a852d1`


| model                         | call_count | input_tokens | output_tokens | thinking_tokens | total_output_tokens | thinking_token_percentage | total_tokens | p50 | p90 | p95 | p99 |
| ----------------------------- | ---------- | ------------ | ------------- | --------------- | ------------------- | ------------------------- | ------------ | --- | --- | --- | --- |
| gemini-3.1-flash-lite-preview | 884        | 5,184,151    | 68,597        | 164,344         | 232,941             | 70.55                     | 5,417,092    | 7   | 15  | 20  | 34  |
| gemini-3-flash-preview        | 40         | 331,071      | 45,534        | 123,301         | 168,835             | 73.03                     | 499,906      | 31  | 49  | 56  | 71  |
| NULL                          | 3          | NULL         | NULL          | NULL            | NULL                | NULL                      | NULL         | 9   | 143 | 143 | 143 |


## Run: GEPA Mermaid gemini-3.1-flash-lite [520]

Trace ID: `019d1f703fd624e9972d5826c8c4d127`


| model                         | call_count | input_tokens | output_tokens | thinking_tokens | total_output_tokens | thinking_token_percentage | total_tokens | p50 | p90 | p95 | p99 |
| ----------------------------- | ---------- | ------------ | ------------- | --------------- | ------------------- | ------------------------- | ------------ | --- | --- | --- | --- |
| gemini-3.1-flash-lite-preview | 1,678      | 9,555,713    | 132,484       | 288,867         | 421,351             | 68.56                     | 9,977,064    | 5   | 15  | 18  | 40  |
| gemini-3-flash-preview        | 74         | 593,763      | 63,015        | 256,324         | 319,339             | 80.27                     | 913,102      | 28  | 52  | 64  | 91  |
| NULL                          | 15         | NULL         | NULL          | NULL            | NULL                | NULL                      | NULL         | 111 | 339 | 349 | 352 |


## Run: GEPA Mermaid gemini-3.1-flash-lite

Trace ID: `019d235ff9b182cc6cd70164688beeef`


| model                         | call_count | input_tokens | output_tokens | thinking_tokens | total_output_tokens | thinking_token_percentage | total_tokens | p50 | p90 | p95 | p99 |
| ----------------------------- | ---------- | ------------ | ------------- | --------------- | ------------------- | ------------------------- | ------------ | --- | --- | --- | --- |
| gemini-3.1-flash-lite-preview | 1,243      | 7,633,875    | 94,922        | 215,955         | 310,877             | 69.47                     | 7,944,752    | 6   | 15  | 21  | 74  |
| gemini-3-flash-preview        | 41         | 392,882      | 35,438        | 155,262         | 190,700             | 81.42                     | 583,582      | 35  | 55  | 70  | 76  |
| NULL                          | 2          | NULL         | NULL          | NULL            | NULL                | NULL                      | NULL         | 146 | 293 | 293 | 293 |


## Notes

- `NULL` model rows indicate spans where model metadata was missing for the selected JSON path.
- Latency percentiles (`p50`, `p90`, `p95`, `p99`) are based on `duration` from `records`.

