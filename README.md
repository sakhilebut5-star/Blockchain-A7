# BLCH9X2 Assignment 7: Consensus and Forks Lab

Nakamoto-consensus simulator with three nodes (A, B, C) that hold divergent block trees,
create forks, and resolve them with the longest-chain rule (heaviest cumulative work as an extension).
All nodes run in one process (in-process simulation, as the brief allows).

## Layout
| Path | Purpose |
|---|---|
| `consensus_lab.ipynb` | Main notebook: full code cell by cell, every scenario, figures, tables, Parts B and C, Word report build (executed, outputs saved) |
| `consensus.py` | Core lab: Tx, Block (real PoW), Node (block tree, orphan pool, fork choice, reorg), Network (discrete-event gossip, latency, bandwidth, partitions), scenarios, confirmation-depth analytics |
| `analysis.py` | Figures (`figures/`) and CSVs (`results/`) for Parts A, B, C |
| `build_docx.py` | Builds both Word reports in `report/`; formulas are written in LaTeX and converted by pandoc into native Word equations |
| `tests/test_consensus.py` | 8 pytest checks, one per report claim |
| `results/` | JSON logs of every scenario, depth-table and relay-policy CSVs |

## Run
```bash
pip install -r requirements.txt
jupyter notebook consensus_lab.ipynb   # run all cells; or use the scripts below
python consensus.py all        # all scenarios, prints event logs, writes results/*.json
python consensus.py fork       # or: tie | delay | heaviest | doublespend --z 2 | depth
python analysis.py
python build_docx.py          # needs pandoc
pytest -q
```

## Scenario map to the brief
| Brief item | Scenario |
|---|---|
| (a) three in-process nodes, divergent chains | every scenario |
| (b) fork created and resolved | `fork` (A3 vs C3, resolved by B4, C reorgs depth 1) |
| Extension: heaviest work vs longest chain | `heaviest` (3 easy blocks vs 2 hard blocks) |
| (c) ties | `tie` (first_seen, lowest_hash, random) |
| (c) delayed arrival | `delay` (orphan pool; partition and late delivery, depth-2 reorg) |
| (c) confirmation depth, R10 000 | `doublespend`, `depth`, `results/depth_table.csv` |
| Part B | `simulate_stale_rate`, `figures/fig3_relay_vs_policy.png`, `fig4_remittance_timeline.png` |
| Part C | memo in `report/Assignment7_PartsB_C_Graduate.docx` |

Assumptions (edit in `analysis.py`): block interval 600 s, tolerated reversal probability 0.1% for Part B.
