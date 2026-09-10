# PR-B7 transport bench results

date: 2026-09-10T05:33:32+00:00  
n_small=200 n_large=60 batch=50×4  
payload: small=~120B large=~64KB padded (read-only sessions method both sides)

| scenario | pattern | result |
|---|---|---|
| REST small (compress=off) | 1 req / conn | p50=4.8ms p95=6.1ms mean=5.2ms ≈193 rps |
| REST large 64KB (compress=off) | 1 req / conn | p50=5.7ms p95=7.9ms mean=5.9ms ≈170 rps |
| Frame all scenarios (compress=off) | persistent stream | BROKEN — no WELCOME/RESULT within 15.0s (bidi chunked POST: uvicorn drops the request body once the response starts; see docs/TRANSPORT_EVALUATION_PR_B7.md) |
| REST small (compress=on) | 1 req / conn | p50=3.9ms p95=4.9ms mean=4.0ms ≈248 rps |
| REST large 64KB (compress=on) | 1 req / conn | p50=4.6ms p95=5.4ms mean=4.6ms ≈218 rps |
| Frame all scenarios (compress=on) | persistent stream | BROKEN — no WELCOME/RESULT within 15.0s (bidi chunked POST: uvicorn drops the request body once the response starts; see docs/TRANSPORT_EVALUATION_PR_B7.md) |
