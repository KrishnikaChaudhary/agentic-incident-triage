Averages over 10 incidents (MockLLM: latency/TTFT simulated, prices are placeholders).

| config | tokens in | tokens out | cost/request | vs baseline | tok/s | TTFT (s) | latency (s) | LLM calls | cache hits | resolved |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline | 4530 | 211 | $0.01676 | +0% | 22 | 0.45 | 9.93 | 14.7 | 0.0 | 100% |
| compact_state | 1704 | 211 | $0.00828 | -51% | 22 | 0.45 | 9.93 | 14.7 | 0.0 | 100% |
| model_routing | 4530 | 211 | $0.00332 | -80% | 48 | 0.18 | 4.75 | 14.7 | 0.0 | 100% |
| tool_scoping_cache | 3626 | 211 | $0.01405 | -16% | 22 | 0.45 | 9.81 | 14.7 | 1.5 | 100% |
| optimized | 1602 | 211 | $0.00183 | -89% | 48 | 0.18 | 4.63 | 14.7 | 1.5 | 100% |
