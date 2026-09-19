# v4.2 migration

1. Replace the repository contents with this package or copy the new builder/workflow files into the existing repo.
2. Run GitHub Actions -> `Build Taiwan ROAD_GRAPH` -> `Run workflow`.
3. Confirm the summary prints v4.2 SCC and Level-2 counts and the R2 upload completes.
4. Do not change Worker bindings. `ROAD_GRAPH`, `SIGNALS` and `TRAFFIC_KV` remain the same.
5. Deploy `worker/Worker_v4.2.0_multilevel_scc.txt` as the complete Worker source.
6. Check `/health` and then test `/route/custom-car`.

Motorcycle `/route` is not changed by v4.2.
