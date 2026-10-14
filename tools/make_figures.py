"""Generate documentation figures from the same data the verifier uses.

    python -m tools.make_figures
"""
from __future__ import annotations

import ipaddress as ipa
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

from tools.design_model import Network  # noqa: E402
from tools.validate_design import LAN_HOSTS, ROOT, SCENARIOS, TEST_A, TEST_B, NORMAL, Design  # noqa: E402
from tools.vlsm import allocate  # noqa: E402

OUT = ROOT / "docs" / "images"
plt.rcParams.update({"font.family": "DejaVu Sans", "figure.dpi": 130, "savefig.bbox": "tight"})

POS = {"NEOC": (0, 2.5), "SMS": (-2.9, 2.5), "FRH": (-2.2, 0.9), "SW": (0, 0.9), "MEC": (2.2, 0.9),
       "CRC": (0, -0.8), "MOP": (-2.9, -0.8), "USR": (3.9, -0.8)}
DIRECT = [("NEOC", "SMS"), ("SMS", "FRH"), ("NEOC", "FRH"), ("NEOC", "MEC"), ("MEC", "USR"), ("CRC", "MOP")]
CENTRAL = ["NEOC", "FRH", "MEC", "CRC"]
CENTRAL_NET = ipa.ip_network("18.50.45.192/29")
GREEN, BLUE, ORANGE, RED = "#27ae60", "#2980b9", "#e67e22", "#c0392b"


def segments(net: Network, trace):
    """Polylines (lists of node names) used by a trace, using the actual egress interface at each hop."""
    segs = []
    for (a, b), iface in zip(zip(trace.hops, trace.hops[1:]), trace.egress):
        a, b = a.replace("Router-", ""), b.replace("Router-", "")
        central = net.routers[f"Router-{a}"].interfaces[iface].address.network == CENTRAL_NET
        segs.append([a, "SW", b] if central else [a, b])
    return segs


def draw_trace(ax, net, src, dst, color, lw=5, offset=0.0, ls="-"):
    t = net.trace(LAN_HOSTS[src], LAN_HOSTS[dst])
    for seg in segments(net, t):
        ax.plot([POS[n][0] + offset for n in seg], [POS[n][1] + offset for n in seg], color=color, lw=lw,
                alpha=0.85, ls=ls, solid_capstyle="round", zorder=3)
    return t


def draw_base(ax, title, down=None):
    for a, b in DIRECT:
        ax.plot([POS[a][0], POS[b][0]], [POS[a][1], POS[b][1]], color="#9aa5ad", lw=2, zorder=1)
    for r in CENTRAL:
        ax.plot([POS[r][0], POS["SW"][0]], [POS[r][1], POS["SW"][1]], color="#9aa5ad", lw=2, zorder=1)
    if down:
        (x1, y1), (x2, y2) = POS[down[0]], POS[down[1]]
        ax.plot([x1, x2], [y1, y2], color=RED, lw=2.6, ls="--", zorder=2)
        ax.text((x1 + x2) / 2, (y1 + y2) / 2, "✕", color=RED, fontsize=20, ha="center", va="center", fontweight="bold", zorder=6)
    for n, (x, y) in POS.items():
        if n == "SW":
            ax.add_patch(FancyBboxPatch((x - 0.4, y - 0.17), 0.8, 0.34, boxstyle="round,pad=0.02", fc="#ecf0f1", ec="#7f8c8d", zorder=4))
            ax.text(x, y, "Central\nSwitch", ha="center", va="center", fontsize=6.5, zorder=6)
        else:
            ax.add_patch(FancyBboxPatch((x - 0.42, y - 0.21), 0.84, 0.42, boxstyle="round,pad=0.03", fc="#2c3e50", ec="#1a252f", zorder=4))
            ax.text(x, y, n, ha="center", va="center", fontsize=9, color="white", fontweight="bold", zorder=6)
    ax.set_xlim(-3.7, 4.6)
    ax.set_ylim(-3.1, 3.2)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title(title, fontsize=10.5, fontweight="bold")


def hop_text(t) -> str:
    return " → ".join(h.replace("Router-", "") for h in t.hops) + ("" if t.ok else "  ✗")


def failover_figure(d: Design) -> None:
    net = {k: Network(d.routers, d.links, v) for k, v in SCENARIOS.items()}
    patched = d.with_patch()
    pnet = Network(patched.routers, patched.links, SCENARIOS[TEST_B])
    fig, axes = plt.subplots(2, 2, figsize=(13.5, 9.6))

    ax = axes[0][0]
    draw_base(ax, "Normal operation")
    n = net[NORMAL]
    t1 = draw_trace(ax, n, "MOP", "NEOC", GREEN, offset=-0.07)
    t2 = draw_trace(ax, n, "FRH", "MEC", BLUE, offset=0.0)
    t3 = draw_trace(ax, n, "MEC", "FRH", ORANGE, lw=3, offset=0.08)
    ax.text(0.4, -1.75, f"MOP→NEOC  {hop_text(t1)}\nFRH→MEC  {hop_text(t2)}\nMEC→FRH (reply)  {hop_text(t3)}", ha="center", va="top", fontsize=8)

    ax = axes[0][1]
    draw_base(ax, "Test A — NEOC↔MEC fibre down (recursive backup)", ("NEOC", "MEC"))
    a = net[TEST_A]
    t1 = draw_trace(ax, a, "MEC", "NEOC", GREEN, offset=-0.06)
    t2 = draw_trace(ax, a, "NEOC", "MEC", ORANGE, lw=3, offset=0.07)
    ax.text(0.4, -1.75, f"MEC→NEOC  {hop_text(t1)}\nNEOC→MEC (reply)  {hop_text(t2)}", ha="center", va="top", fontsize=8)

    ax = axes[1][0]
    draw_base(ax, "Test B — NEOC central port down (floating route + IP SLA)", ("NEOC", "SW"))
    b = net[TEST_B]
    t1 = draw_trace(ax, b, "CRC", "NEOC", GREEN, offset=-0.06)
    t2 = b.trace(LAN_HOSTS["NEOC"], LAN_HOSTS["CRC"])
    ax.text(0.4, -1.75, f"CRC→NEOC  {hop_text(t1)}\nNEOC→CRC (reply)  ✗ {t2.reason.split(' at ')[0]} at NEOC", ha="center", va="top", fontsize=8, color="#222")
    ax.annotate("no return route", xy=(0, 2.5), xytext=(0.9, 3.0), color=RED, fontsize=9, fontweight="bold")

    ax = axes[1][1]
    draw_base(ax, "Test B with the optional NEOC patch", ("NEOC", "SW"))
    t1 = draw_trace(ax, pnet, "CRC", "NEOC", GREEN, offset=-0.06)
    t2 = draw_trace(ax, pnet, "NEOC", "CRC", ORANGE, lw=3, offset=0.07)
    ax.text(0.4, -1.75, f"CRC→NEOC  {hop_text(t1)}\nNEOC→CRC (reply)  {hop_text(t2)}", ha="center", va="top", fontsize=8)

    fig.suptitle("QuakeNet failover — paths computed by the design verifier", fontsize=13.5, y=0.94)
    fig.subplots_adjust(hspace=0.12, wspace=0.05)
    fig.savefig(OUT / "failover_paths.png")
    plt.close(fig)


def address_map(d: Design) -> None:
    plan = allocate("18.50.0.0/16", d.requirements)
    palette = ["#2c3e50", "#2980b9", "#3498db", "#16a085", "#27ae60", "#f39c12", "#8e44ad", "#c0392b",
               "#d35400", "#7f8c8d", "#34495e", "#1abc9c", "#e74c3c", "#95a5a6"]
    fig, ax = plt.subplots(figsize=(11, 5.8))
    for i, p in enumerate(plan):
        y = len(plan) - 1 - i
        size = p["subnet"].num_addresses
        ax.barh(y, size, color=palette[i % len(palette)], height=0.62)
        ax.text(size * 1.15, y, f"{p['subnet']}  ·  {p['usable_hosts']} usable  ·  need {p['required_hosts']}", va="center", fontsize=8.5)
    ax.set_xscale("log")
    ax.set_xlim(2, 400000)
    ax.set_yticks(range(len(plan)))
    ax.set_yticklabels([p["network"] for p in plan][::-1], fontsize=8.5)
    ax.set_xlabel("Block size (addresses, log scale)")
    ax.set_title("VLSM allocation of 18.50.0.0/16 — largest demand first, no overlaps\n"
                 "(used: 18.50.0.0 – 18.50.45.223; the rest of the /16 is free for expansion)", fontsize=11)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="x", alpha=0.25)
    fig.savefig(OUT / "vlsm_address_map.png")
    plt.close(fig)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    d = Design()
    failover_figure(d)
    address_map(d)
    print("wrote", OUT / "failover_paths.png", "and", OUT / "vlsm_address_map.png")


if __name__ == "__main__":
    main()
