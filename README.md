# Taiwan ROAD_GRAPH v4.2

v4.2 keeps the v1 detail graph and v4.1 compact CSR geometry format, then adds two routing-control layers:

- `components.bin`: strongly-connected-component id for every L1 overlay node.
- `level2.json`: coarse 0.25° directed region graph used only as a guide.

Runtime behavior:

1. Short routes (<=15 km straight-line) use the proven bounded detail tier directly.
2. Medium/long routes pick start/end overlay candidates in the same SCC, preventing the old 79k-node "unreachable goal" scans.
3. Level-2 finds a coarse region path.
4. L1 first searches near that region path, then performs an unrestricted SCC proof search with the guided route cost as an upper bound. This keeps the L1 result optimal for its static edge costs while reducing search space.
5. L0 detail refinement is staged instead of one giant corridor.
6. HERE/TDX traffic, post-route signals and motorcycle `/route` are untouched.

R2 layout:

- `graph/v1/TW/...`
- `graph/v4.2/TW/index.json`
- `graph/v4.2/TW/topology.bin`
- `graph/v4.2/TW/components.bin`
- `graph/v4.2/TW/level2.json`
- `graph/v4.2/TW/geometry/*.bin`
