"""
analysis.py
Generates every figure and CSV used in the report (Parts A, B, C).
Greyscale only. Run after (or independently of) consensus.py.

    python analysis.py
"""
import csv
import math
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

import consensus as cs

HERE = os.path.dirname(os.path.abspath(__file__))
FIG = os.path.join(HERE, "figures")
RES = os.path.join(HERE, "results")
os.makedirs(FIG, exist_ok=True)
os.makedirs(RES, exist_ok=True)

plt.rcParams.update({"font.family": "DejaVu Serif", "font.size": 9, "axes.edgecolor": "black",
                     "axes.linewidth": 0.7, "axes.grid": True, "grid.color": "0.88",
                     "grid.linewidth": 0.5, "legend.frameon": False, "savefig.dpi": 220})

# Assumptions (edit here)
T_BTC = 600.0                     # Bitcoin target block interval, seconds
DW_MEDIAN, DW_MEAN, DW_P95 = 6.5, 12.6, 40.0   # Decker & Wattenhofer (2013) propagation stats
EPS = 1e-3                        # tolerated double-spend probability


def save(fig, name):
    fig.savefig(os.path.join(FIG, name), bbox_inches="tight")
    plt.close(fig)


# --------------------------------------------------------------------------- #
# Figure 1: block tree from the fork scenario
# --------------------------------------------------------------------------- #
def fig_block_tree():
    fig, ax = plt.subplots(figsize=(7.0, 2.3))
    ax.set_xlim(-0.3, 5.6); ax.set_ylim(-1.5, 1.0); ax.axis("off")
    main = [("G", 0), ("A1", 1), ("B2", 2), ("A3", 3), ("B4", 4)]
    pos = {n: (x, 0) for n, x in main}
    pos["C3"] = (3, -1)

    def box(n, fc, hatch=None):
        x, y = pos[n]
        ax.add_patch(FancyBboxPatch((x - 0.3, y - 0.22), 0.6, 0.44, boxstyle="round,pad=0.02",
                                    fc=fc, ec="black", lw=0.8, hatch=hatch))
        ax.text(x, y, n, ha="center", va="center", fontsize=9)

    for n, _ in main:
        box(n, "white")
    box("C3", "0.8")
    for a, b in [("G", "A1"), ("A1", "B2"), ("B2", "A3"), ("A3", "B4"), ("B2", "C3")]:
        (x1, y1), (x2, y2) = pos[a], pos[b]
        ax.annotate("", xy=(x2 - 0.3, y2), xytext=(x1 + 0.3, y1),
                    arrowprops=dict(arrowstyle="-|>", lw=0.8, color="black"))
    ax.text(4.45, -1.0, "C3: stale (orphaned) at height 3.\nC reorganises at t = 32 s\n"
                        "(depth 1) when B4 arrives.", fontsize=8, va="center")
    ax.text(3.0, 0.55, "t = 19 s: A3 and C3 mined\nsimultaneously on B2", ha="center", fontsize=8)
    ax.text(0.0, -1.0, "Tips at t = 29 s (first-seen):\nA, B on A3 | C on C3", fontsize=8,
            va="center")
    save(fig, "fig1_block_tree.png")


# --------------------------------------------------------------------------- #
# Figure 2: attacker success vs confirmations
# --------------------------------------------------------------------------- #
def fig_depth():
    rows = []
    fig, ax = plt.subplots(figsize=(6.2, 3.3))
    styles = {0.10: ("black", "o"), 0.20: ("0.35", "s"), 0.30: ("0.6", "^")}
    zs = list(range(1, 13))
    for q, (c, m) in styles.items():
        r = [cs.rosenfeld_success(q, z) for z in zs]
        n = [cs.nakamoto_success(q, z) for z in zs]
        mc = [cs.attacker_success_mc(q, z, 20_000, seed=z, premine=1) for z in zs]
        s = [cs.strict_success(q, z) for z in zs]
        ax.semilogy(zs, r, color=c, lw=1.2, label=f"Rosenfeld, q = {q:.1f}")
        ax.semilogy(zs, n, color=c, lw=0.9, ls="--")
        ax.semilogy([z for z, v in zip(zs, mc) if v > 0], [v for v in mc if v > 0], color=c, ls="none",
                    marker=m, ms=3.5)
        for z, a, b, d, e in zip(zs, r, n, mc, s):
            rows.append([q, z, round(b, 7), round(a, 7), round(d, 7), round(e, 7)])
    ax.axhline(EPS, color="black", lw=0.6, ls=":")
    ax.text(12.1, EPS, "0.1%", va="center", fontsize=7)
    ax.set_xlabel("Confirmations z"); ax.set_ylabel("P(double-spend succeeds)")
    ax.set_ylim(1e-6, 1); ax.set_xticks(zs)
    ax.legend(fontsize=7, loc="lower left")
    ax.text(0.99, 0.97, "solid: Rosenfeld (2014)   dashed: Nakamoto (2008)\nmarkers: Monte Carlo "
            "(20 000 races each)", transform=ax.transAxes, ha="right", va="top", fontsize=7)
    save(fig, "fig2_confirmation_depth.png")
    with open(os.path.join(RES, "depth_table.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["q", "z", "nakamoto", "rosenfeld_premine1", "mc_premine1", "strict_premine0"])
        w.writerows(rows)


# --------------------------------------------------------------------------- #
# Part B: relay time vs confirmation policy
# --------------------------------------------------------------------------- #
def stale_formula(d, T, n=10):
    return 1 - math.exp(-(1 - 1 / n) * d / T)


_SIM_CACHE = {}


def sim_stale(T, d):
    key = (T, d)
    if key not in _SIM_CACHE:
        _SIM_CACHE[key] = cs.simulate_stale_rate(T, d, n_miners=10, n_blocks=12_000,
                                                 seed=int(d * 10) + int(T))
    return _SIM_CACHE[key]


def policy(q, d, T, eps=EPS, f=None):
    """Honest work lost to forks raises the attacker's effective share:
    q_eff = q / (q + p(1 - f)). Returns (f, q_eff, z*, expected wait in minutes);
    z* is None when q_eff >= 0.5 or no depth up to 200 meets eps."""
    f = (1 - math.exp(-d / T)) if f is None else f
    q_eff = q / (q + (1 - q) * (1 - f))
    if q_eff >= 0.5:
        return f, q_eff, None, None
    z = cs.required_depth(q_eff, eps)
    if z >= 200:
        return f, q_eff, None, None
    return f, q_eff, z, z * T / (1 - f) / 60.0


def fig_relay():
    delays = [0.5, 1, 2, 5, 10, 20, 40, 60, 90, 120]
    rows = []
    fig, axs = plt.subplots(1, 3, figsize=(7.4, 2.7))
    ax = axs[0]
    for T, c, m, lab in [(600, "black", "o", "T = 600 s (Bitcoin)"), (60, "0.5", "s", "T = 60 s")]:
        sim = [sim_stale(T, d) for d in delays]
        xs = [x / 10 for x in range(5, 1201)]
        ax.loglog(xs, [stale_formula(x, T) for x in xs], color=c, lw=1)
        ax.loglog(delays, [max(v, 1e-4) for v in sim], color=c, marker=m, ls="none", ms=3.5, label=lab)
        for d, v in zip(delays, sim):
            rows.append(["stale", T, d, round(v, 5), round(stale_formula(d, T), 5), "", "", ""])
    ax.set_xlabel("Block relay time d (s)"); ax.set_ylabel("Stale (fork) rate")
    ax.set_title("(a) Forks rise with relay time", fontsize=8.5)
    ax.legend(fontsize=6.5, loc="upper left")

    for q, c in [(0.10, "black"), (0.25, "0.5")]:
        for T, ls in [(600, "-"), (60, "--")]:
            zs, ws = [], []
            for d in delays:
                f, qe, z, w = policy(q, d, T, f=sim_stale(T, d))
                zs.append(z if z is not None else float("nan"))
                ws.append(w if w is not None else float("nan"))
                rows.append(["policy", T, d, round(f, 5), "", q, z if z else "unsafe",
                             round(w, 1) if w else "unsafe"])
            axs[1].semilogx(delays, zs, color=c, ls=ls, marker="o", ms=2.5,
                            label=f"q = {q:.2f}, T = {int(T)} s")
            axs[2].semilogx(delays, ws, color=c, ls=ls, marker="o", ms=2.5)
    axs[1].set_yscale("log")
    axs[1].set_xlabel("Block relay time d (s)"); axs[1].set_ylabel("Confirmations for P < 0.1%")
    axs[1].set_title("(b) Required depth z*", fontsize=8.5)
    axs[1].legend(fontsize=6, loc="upper left")
    axs[2].set_yscale("log")
    axs[2].set_xlabel("Block relay time d (s)"); axs[2].set_ylabel("Expected wait (minutes)")
    axs[2].set_title("(c) Time to accept remittance", fontsize=8.5)
    for ax in axs:
        for x, ls in [(DW_MEDIAN, ":"), (DW_P95, "--")]:
            ax.axvline(x, color="0.4", lw=0.6, ls=ls)
    fig.text(0.5, -0.04, "(a) lines: 1 - exp(-0.9d/T); markers: 10-miner simulation. (b) a line ends where no depth "
             "up to 200 gives P < 0.1% (effective q near 0.5).\nDotted and dashed verticals: median "
             "(6.5 s) and 95th percentile (40 s) block propagation reported by Decker and Wattenhofer "
             "(2013).", ha="center", fontsize=6.5)
    fig.tight_layout()
    save(fig, "fig3_relay_vs_policy.png")
    with open(os.path.join(RES, "relay_policy.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["kind", "T", "delay_s", "stale_sim_or_f", "stale_formula", "q", "z_req", "wait_min"])
        w.writerows(rows)


def fig_remittance_timeline():
    """Schematic: where relay time enters a remittance under each policy."""
    fig, ax = plt.subplots(figsize=(7.2, 2.4))
    ax.set_xlim(-2, 70); ax.set_ylim(-0.5, 4.6); ax.axis("off")
    lanes = [("Sender wallet broadcast", 0, 0.1, "white"),
             ("Tx relay to miners (~seconds)", 0.1, 0.4, "0.9"),
             ("First block found (Exp, mean 10 min)", 0.4, 10, "0.75"),
             ("Block relay d (6.5 s median, 40 s p95; widened)", 10, 10.7, "0.45"),
             ("Further confirmations (each ~10 min)", 10.7, 60, "0.9")]
    for i, (lab, a, b, fc) in enumerate(lanes):
        y = 4 - i * 0.85
        ax.add_patch(FancyBboxPatch((a, y - 0.25), max(b - a, 0.4), 0.5, boxstyle="square,pad=0",
                                    fc=fc, ec="black", lw=0.6))
        ax.text(-1.5, y, "", fontsize=7)
        ax.text(b + 0.8 if b < 50 else 20, y, lab, va="center", fontsize=7.5)
    for z, x in [(0, 0.4), (1, 10.7), (3, 30.7), (6, 60.7)]:
        ax.axvline(x, color="black", lw=0.5, ls=":")
        ax.text(x, -0.35, f"z={z}", ha="center", fontsize=7)
    ax.text(35, 4.45, "Minutes after broadcast (Bitcoin, T = 600 s; schematic, not to scale)", ha="center", fontsize=8)
    save(fig, "fig4_remittance_timeline.png")


if __name__ == "__main__":
    fig_block_tree(); fig_depth(); fig_relay(); fig_remittance_timeline()
    for q in (0.10, 0.25):
        for d in (DW_MEDIAN, DW_MEAN, DW_P95):
            print(q, d, policy(q, d, T_BTC), policy(q, d, 60))
