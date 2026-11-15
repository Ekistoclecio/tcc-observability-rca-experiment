# Existing F4/F5 Reprocessing

Offline reprocessing of existing immutable RAW snapshots. No fault was executed and no RAW input was modified.

## F4 — `iv-edv-so-115b-001-f4-f4`

Overall gate: **FAIL**

### Scenario A

- Explicit marked shared events in RAW: 0
- Marked event names: `{}`
- Model-visible counts: metrics 103, logs 0, traces 110, spans 2656
- Provenance: PASS; allowed removals 103; unexpected removals/additions/mutations 0/0/0; evidence IDs PASS

### Scenario B

- Explicit marked shared events in RAW: 0
- Marked event names: `{}`
- Model-visible counts: metrics 59, logs 0, traces 110, spans 2656
- Provenance: PASS; allowed removals 59; unexpected removals/additions/mutations 0/0/0; evidence IDs PASS

Parity:

- logs: PASS (A-only=0, B-only=0)
- metrics: FAIL (A-only=44, B-only=0)
- traces: PASS (A-only=0, B-only=0)

A-only metric keys (sample):

- `['request_count', 'adservice', 'AdService/GetAds', 'server', 'success', None]`
- `['request_count', 'cartservice', 'CartService/AddItem', 'server', 'success', None]`
- `['request_count', 'cartservice', 'CartService/EmptyCart', 'server', 'success', None]`
- `['request_count', 'cartservice', 'CartService/GetCart', 'server', 'success', None]`
- `['request_count', 'cartservice', 'redis GET', 'datastore', 'success', 'redis-cart']`
- `['request_count', 'cartservice', 'redis GET/SET', 'datastore', 'success', 'redis-cart']`
- `['request_count', 'cartservice', 'redis SET', 'datastore', 'success', 'redis-cart']`
- `['request_count', 'frontend', 'AdService/GetAds', 'client', 'success', 'adservice']`

Decision: this snapshot predates explicit shared-observation markers and/or paired adapter collection. A rendered event name is not treated as proof of a shared source point. No message-specific inference was applied.

## F5 — `iv-edv-so-115b-002-f5-f5`

Overall gate: **FAIL**

### Scenario A

- Explicit marked shared events in RAW: 0
- Marked event names: `{}`
- Model-visible counts: metrics 103, logs 0, traces 94, spans 2329
- Provenance: PASS; allowed removals 103; unexpected removals/additions/mutations 0/0/0; evidence IDs PASS

### Scenario B

- Explicit marked shared events in RAW: 0
- Marked event names: `{}`
- Model-visible counts: metrics 55, logs 0, traces 94, spans 2329
- Provenance: PASS; allowed removals 55; unexpected removals/additions/mutations 0/0/0; evidence IDs PASS

Parity:

- logs: PASS (A-only=0, B-only=0)
- metrics: FAIL (A-only=48, B-only=0)
- traces: PASS (A-only=0, B-only=0)

A-only metric keys (sample):

- `['request_count', 'adservice', 'AdService/GetAds', 'server', 'success', None]`
- `['request_count', 'cartservice', 'CartService/AddItem', 'server', 'success', None]`
- `['request_count', 'cartservice', 'CartService/EmptyCart', 'server', 'success', None]`
- `['request_count', 'cartservice', 'CartService/GetCart', 'server', 'error', None]`
- `['request_count', 'cartservice', 'CartService/GetCart', 'server', 'success', None]`
- `['request_count', 'cartservice', 'redis GET', 'datastore', 'error', 'redis-cart']`
- `['request_count', 'cartservice', 'redis GET', 'datastore', 'success', 'redis-cart']`
- `['request_count', 'cartservice', 'redis GET/SET', 'datastore', 'success', 'redis-cart']`

Decision: this snapshot predates explicit shared-observation markers and/or paired adapter collection. A rendered event name is not treated as proof of a shared source point. No message-specific inference was applied.

## Gate result

**FAIL**. The reprocess gate failed; therefore no fresh F4/F5 or F1–F5 physical run was started.
