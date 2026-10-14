#!/usr/bin/env python3
"""Design verifier for QuakeNet.

Cross-checks the addressing plan, device tables, router configs and service settings against each
other, then simulates routing and forwarding in normal operation and under both failover tests
(Test A: NEOC-MEC fibre down; Test B: NEOC central-switch port down, which exercises IP SLA tracking).
Exits non-zero if any error-level check fails; findings that describe known gaps are warnings.

    python -m tools.validate_design
"""
from __future__ import annotations

import csv
import ipaddress as ipa
import sys
from dataclasses import dataclass
from pathlib import Path

from tools.design_model import Network, Router, load_links, load_routers, parse_router
from tools.vlsm import allocate, load_requirements

ROOT = Path(__file__).resolve().parents[1]
BASE = "18.50.0.0/16"
FLOATING_AD = 48
DNS_IP = "18.50.0.2"

# one representative host address per LAN (used as traffic source / destination)
LAN_HOSTS = {"NEOC": "18.50.0.50", "USR": "18.50.32.50", "MEC": "18.50.40.50", "FRH": "18.50.42.50",
             "SMS": "18.50.44.50", "CRC": "18.50.45.50", "MOP": "18.50.45.150"}
# DNS, both web servers and all seven mail servers
SERVICE_TARGETS = {"DNS": DNS_IP, "WEB-NEOC": "18.50.0.3", "WEB-MEC": "18.50.40.2", "MAIL-NEOC": "18.50.0.4",
                   "MAIL-MEC": "18.50.40.3", "MAIL-SMS": "18.50.44.2", "MAIL-FRH": "18.50.42.2",
                   "MAIL-USR": "18.50.32.2", "MAIL-CRC": "18.50.45.2", "MAIL-MOP": "18.50.45.130"}

NORMAL = "normal"
TEST_A = "A: NEOC-MEC fibre down (Router-NEOC Gi0/0/2 shutdown)"
TEST_B = "B: NEOC central-switch port down (Router-NEOC Gi0/0/1 shutdown)"
SCENARIOS = {NORMAL: set(),
             TEST_A: {("Router-NEOC", "GigabitEthernet0/0/2")},
             TEST_B: {("Router-NEOC", "GigabitEthernet0/0/1")}}


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""
    level: str = "error"          # "error" fails the run, "warn" is a finding / informational

    @property
    def mark(self) -> str:
        return "PASS" if self.passed else ("FAIL" if self.level == "error" else "WARN")


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


class Design:
    """All design inputs loaded from the repository (optionally overridden for testing)."""

    def __init__(self, root: Path = ROOT, routers: dict[str, Router] | None = None):
        self.root = root
        self.routers = routers if routers is not None else load_routers(root / "configs" / "routers")
        self.links = load_links(root / "addressing" / "physical_connections.csv")
        self.plan = read_csv(root / "addressing" / "vlsm_plan.csv")
        self.requirements = load_requirements(root / "addressing" / "vlsm_requirements.csv")
        self.iface_rows = read_csv(root / "addressing" / "router_interfaces.csv")
        self.devices = read_csv(root / "addressing" / "devices.csv")
        self.dhcp_servers = read_csv(root / "services" / "dhcp_servers.csv")
        self.dns = read_csv(root / "services" / "dns_records.csv")

    def router_text(self, name: str) -> str:
        return (self.root / "configs" / "routers" / f"{name}.txt").read_text()

    def with_router(self, name: str, text: str) -> "Design":
        from copy import copy
        d = copy(self)
        d.routers = dict(self.routers)
        d.routers[name] = parse_router(text)
        return d

    def with_patch(self) -> "Design":
        patch = (self.root / "configs" / "patches" / "NEOC_testB_return_path.txt").read_text()
        return self.with_router("Router-NEOC", self.router_text("Router-NEOC") + "\n" + patch)


# ------------------------------------------------------------------------------ checks
def check_vlsm(d: Design) -> list[Check]:
    computed = allocate(BASE, d.requirements)
    mismatches = []
    for want, got in zip(d.plan, computed):
        if (want["network"], want["subnet"]) != (got["network"], str(got["subnet"])):
            mismatches.append(f"{want['network']}: plan {want['subnet']} vs computed {got['subnet']}")
        elif want["broadcast"] != str(got["broadcast"]) or want["first_usable"] != str(got["first_usable"]):
            mismatches.append(f"{want['network']}: usable range / broadcast mismatch")
    nets = [ipa.ip_network(r["subnet"]) for r in d.plan]
    overlaps = [(str(a), str(b)) for i, a in enumerate(nets) for b in nets[i + 1:] if a.overlaps(b)]
    need = dict(d.requirements)
    short = [r["network"] for r in d.plan if ipa.ip_network(r["subnet"]).num_addresses - 2 < need.get(r["network"], 0)]
    last = max(n.broadcast_address for n in nets)
    return [
        Check("VLSM plan equals a fresh largest-first allocation of the requirements",
              not mismatches and len(d.plan) == len(computed), "; ".join(mismatches) or f"{len(nets)} subnets"),
        Check("No overlapping subnets in the plan", not overlaps, str(overlaps) if overlaps else "none"),
        Check("Every subnet satisfies its host requirement", not short and set(need) == {r["network"] for r in d.plan},
              ", ".join(short) or f"space used: 18.50.0.0 - {last}"),
    ]


def subnet_of(d: Design, ip: ipa.IPv4Address):
    for r in d.plan:
        n = ipa.ip_network(r["subnet"])
        if ip in n:
            return n, r["network"]
    return None, None


def check_addresses(d: Design) -> list[Check]:
    seen: dict[ipa.IPv4Address, str] = {}
    problems, dups = [], []
    entries = [(f"{rn}:{i.name}", i.address) for rn, r in d.routers.items()
               for i in r.interfaces.values() if i.address]
    entries += [(row["device"], ipa.ip_interface(f"{row['ip_address']}/{row['subnet_mask']}")) for row in d.devices]
    for label, intf in entries:
        net, _ = subnet_of(d, intf.ip)
        if net is None:
            problems.append(f"{label} {intf.ip} is outside the plan")
        else:
            if intf.network != net:
                problems.append(f"{label} mask /{intf.network.prefixlen} does not match plan {net}")
            if intf.ip in (net.network_address, net.broadcast_address):
                problems.append(f"{label} uses network/broadcast address")
        if intf.ip in seen:
            dups.append(f"{intf.ip}: {seen[intf.ip]} and {label}")
        seen[intf.ip] = label
    return [Check("All interface and device addresses are valid hosts inside the plan", not problems,
                  "; ".join(problems) or f"{len(entries)} addresses"),
            Check("No duplicate IP addresses across routers, servers, printers and switches", not dups,
                  "; ".join(dups) or "all unique")]


def check_tables_match_configs(d: Design) -> list[Check]:
    bad = []
    for row in d.iface_rows:
        r = d.routers.get(row["router"])
        i = r.interfaces.get(row["interface"]) if r else None
        want = ipa.ip_interface(f"{row['ip_address']}/{row['subnet_mask']}")
        if i is None or i.address != want:
            bad.append(f"{row['router']} {row['interface']}")
    cfg_count = sum(1 for r in d.routers.values() for i in r.interfaces.values() if i.address)
    dns_bad = [r["name"] for r in d.dns if not any(
        row["ip_address"] == r["address"] for row in d.devices)]
    return [Check("Router interface table matches the router configurations", not bad and cfg_count == len(d.iface_rows),
                  ", ".join(bad) or f"{cfg_count} interfaces"),
            Check("Every DNS record points at a real device address", not dns_bad,
                  ", ".join(dns_bad) or f"{len(d.dns)} records")]


def check_gateways(d: Design) -> list[Check]:
    routers_on: dict[ipa.IPv4Network, set[str]] = {}
    for r in d.routers.values():
        for i in r.interfaces.values():
            if i.address:
                routers_on.setdefault(i.address.network, set()).add(str(i.address.ip))
    bad = []
    for row in d.devices:
        net = ipa.ip_interface(f"{row['ip_address']}/{row['subnet_mask']}").network
        if row["default_gateway"] not in routers_on.get(net, set()):
            bad.append(row["device"])
        if row["dns_server"] and row["dns_server"] != DNS_IP:
            bad.append(f"{row['device']} (DNS)")
    return [Check("Every device's default gateway is a router interface on its subnet; DNS = 18.50.0.2",
                  not bad, ", ".join(bad) or f"{len(d.devices)} devices")]


def check_dhcp(d: Design) -> list[Check]:
    neoc = d.routers["Router-NEOC"]
    static_ips = [ipa.ip_address(r["ip_address"]) for r in d.devices]
    static_ips += [i.address.ip for r in d.routers.values() for i in r.interfaces.values() if i.address]

    uncovered, gw_bad = [], []
    for pool in neoc.pools:
        for ip in static_ips:
            if ip in pool["network"] and not any(lo <= ip <= hi for lo, hi in neoc.excluded):
                uncovered.append(f"{ip} in {pool['name']}")
        gw = [i.address for r in d.routers.values() for i in r.interfaces.values()
              if i.address and i.address.ip == pool["default_router"]]
        if not gw or gw[0].network != pool["network"]:
            gw_bad.append(pool["name"])

    servers = {}
    for s in d.dhcp_servers:
        net = ipa.ip_network(f"{s['start_ip']}/{s['subnet_mask']}", strict=False)
        servers.setdefault(s["ip_address"], []).append((s, net))
    router_ips = {i.address.ip: rn for rn, r in d.routers.items() for i in r.interfaces.values() if i.address}
    relay_bad, relays = [], 0
    for rn, r in d.routers.items():
        for i in r.interfaces.values():
            for h in i.helper:
                relays += 1
                giaddr = i.address.ip
                if str(h) in servers:
                    ok = any(giaddr in net and ipa.ip_address(s["default_gateway"]) == giaddr for s, net in servers[str(h)])
                elif h in router_ips:
                    ok = any(giaddr in p["network"] and p["default_router"] == giaddr
                             for p in d.routers[router_ips[h]].pools)
                else:
                    ok = False
                if not ok:
                    relay_bad.append(f"{rn} {i.name} -> {h}")

    srv_bad, srv_warn = [], []
    need = {n: q for n, q in d.requirements}
    for s in d.dhcp_servers:
        start = ipa.ip_address(s["start_ip"])
        net = ipa.ip_network(f"{s['start_ip']}/{s['subnet_mask']}", strict=False)
        end = start + int(s["max_users"]) - 1
        if end not in net or any(start <= ip <= end for ip in static_ips if ip in net):
            srv_bad.append(s["pool_name"])
        lan = next((p["network"] for p in d.plan if ipa.ip_network(p["subnet"]) == net), None)
        if lan and int(s["max_users"]) < need[lan]:
            srv_warn.append(f"{s['pool_name']}: {s['max_users']} < {need[lan]} planned hosts")
    return [
        Check("NEOC DHCP exclusions cover every static address inside its pools", not uncovered, "; ".join(uncovered) or "ok"),
        Check("NEOC DHCP pools use the correct gateway and subnet", not gw_bad, ", ".join(gw_bad) or f"{len(neoc.pools)} pools"),
        Check("Every ip helper-address reaches a DHCP source that serves the relayed LAN (router pool or server pool)",
              not relay_bad and relays == 4, "; ".join(relay_bad) or f"{relays} relay agents"),
        Check("MEC/CRC server pools stay inside their subnet and avoid static addresses", not srv_bad,
              ", ".join(srv_bad) or f"{len(d.dhcp_servers)} pools"),
        Check("DHCP server pool size vs. planned host count", not srv_warn, "; ".join(srv_warn) or "ok", level="warn"),
    ]


def check_static_design(d: Design) -> list[Check]:
    bad_ad, bad_hop, bad_track = [], [], []
    for rn, r in d.routers.items():
        connected = [i.address.network for i in r.interfaces.values() if i.address]
        for s in r.statics:
            if s.ad not in (1, FLOATING_AD):
                bad_ad.append(f"{rn} {s.network} AD {s.ad}")
            if s.is_next_hop and not any(ipa.ip_address(s.target) in n for n in connected):
                bad_hop.append(f"{rn} {s.network} via {s.target}")
            if not s.is_next_hop and s.target not in r.interfaces:
                bad_hop.append(f"{rn} {s.network} -> {s.target}")
            if s.track is not None and (s.track not in r.tracks or r.tracks[s.track] not in r.sla):
                bad_track.append(f"{rn} {s.network} track {s.track}")
    floating_without_primary = []
    for rn, r in d.routers.items():
        prim = {s.network for s in r.statics if s.ad == 1}
        floating_without_primary += [f"{rn} {s.network}" for s in r.statics if s.ad == FLOATING_AD and s.network not in prim]
    no_default = not any(s.network.prefixlen == 0 for r in d.routers.values() for s in r.statics)
    styles = {
        "MEC next-hop only": all(s.is_next_hop for s in d.routers["Router-MEC"].statics),
        "CRC next-hop only": all(s.is_next_hop for s in d.routers["Router-CRC"].statics),
        "MOP next-hop only": all(s.is_next_hop for s in d.routers["Router-MOP"].statics),
        "USR exit-interface only": not any(s.is_next_hop for s in d.routers["Router-USR"].statics)
                                   and len(d.routers["Router-USR"].statics) == 12,
    }
    rip_on = ("Router-NEOC", "Router-SMS", "Router-FRH")
    rip_ok = all(d.routers[n].rip and d.routers[n].rip.version == 2 and not d.routers[n].rip.auto_summary for n in rip_on)
    rip_off = all(d.routers[n].rip is None for n in d.routers if n not in rip_on)
    return [
        Check(f"Every backup (floating) static route uses administrative distance {FLOATING_AD}", not bad_ad, "; ".join(bad_ad) or "ok"),
        Check("Every static route's next hop / exit interface belongs to a directly connected network", not bad_hop,
              "; ".join(bad_hop) or "ok"),
        Check("Every floating route has a primary route for the same prefix", not floating_without_primary,
              "; ".join(floating_without_primary) or "ok"),
        Check("Tracked routes reference a defined IP SLA probe", not bad_track, "; ".join(bad_track) or "ok"),
        Check("No default route (0.0.0.0/0) exists on any router", no_default, "ok"),
        Check("Static styles: MEC/CRC/MOP recursive next-hop, USR exit-interface (12 routes)", all(styles.values()), str(styles)),
        Check("RIPv2 with no auto-summary on NEOC/SMS/FRH only; NEOC redistributes static", rip_ok and rip_off
              and d.routers["Router-NEOC"].rip.redistribute_static, "ok"),
    ]


def flows(net: Network):
    """(label, source ip, destination ip) for every LAN pair and every LAN -> service."""
    out = [(f"{a}->{b}", LAN_HOSTS[a], LAN_HOSTS[b]) for a in LAN_HOSTS for b in LAN_HOSTS if a != b]
    out += [(f"{a}->{s}", LAN_HOSTS[a], ip) for a in LAN_HOSTS for s, ip in SERVICE_TARGETS.items()]
    return out


def run_flows(net: Network):
    fl = flows(net)
    failures = [(label, net.trace(src, dst).reason) for label, src, dst in fl if not net.trace(src, dst).ok]
    return len(fl), failures


def check_dhcp_relay_paths(d: Design) -> Check:
    bad = []
    servers = {s["ip_address"]: s for s in d.dhcp_servers}
    for label, down in SCENARIOS.items():
        net = Network(d.routers, d.links, down)
        for rn, r in d.routers.items():
            for i in r.interfaces.values():
                for h in i.helper:
                    if not net.trace_from(rn, str(h)).ok:
                        bad.append(f"[{label[:1]}] {rn}->{h} (request)")
                    server_gw = str(h) in servers
                    reply_from = net.gateway_of(str(h)) if server_gw else net.ip_owner[h][0]
                    if not net.trace_from(reply_from, str(i.address.ip)).ok:
                        bad.append(f"[{label[:1]}] {reply_from}->{i.address.ip} (reply)")
    return Check("DHCP relay: requests reach the DHCP source and replies return to every relay agent (all 3 states)",
                 not bad, "; ".join(bad) or "ok")


def check_reachability(d: Design) -> list[Check]:
    out = []
    for label in (NORMAL, TEST_A):
        net = Network(d.routers, d.links, SCENARIOS[label])
        total, failures = run_flows(net)
        out.append(Check(f"Full reachability, scenario {label}", not failures,
                         "; ".join(f"{l}: {r}" for l, r in failures[:3]) or f"{total} flows (LAN pairs + DNS/web/mail) delivered"))
    net = Network(d.routers, d.links, SCENARIOS[TEST_B])
    total, failures = run_flows(net)
    dsts = sorted({l.split("->")[1] for l, _ in failures})
    srcs = sorted({l.split("->")[0] for l, _ in failures})
    out.append(Check(f"Full reachability, scenario {TEST_B}", not failures,
                     f"{len(failures)} of {total} flows fail; destinations {dsts}, sources {srcs} "
                     "(replies need a route NEOC loses with its central port)", level="warn"))
    return out


def path(net: Network, src: str, dst: str) -> list[str]:
    return [h.replace("Router-", "") for h in net.trace(LAN_HOSTS[src], LAN_HOSTS[dst]).hops]


def check_expected_paths(d: Design) -> list[Check]:
    n = Network(d.routers, d.links)
    a = Network(d.routers, d.links, SCENARIOS[TEST_A])
    b = Network(d.routers, d.links, SCENARIOS[TEST_B])
    expect = [
        ("normal: MOP -> NEOC goes MOP, CRC, NEOC", path(n, "MOP", "NEOC"), ["MOP", "CRC", "NEOC"]),
        ("normal: FRH -> MEC forward path goes through NEOC (serial, then fibre)", path(n, "FRH", "MEC"), ["FRH", "NEOC", "MEC"]),
        ("normal: MEC -> FRH reply uses the central segment directly", path(n, "MEC", "FRH"), ["MEC", "FRH"]),
        ("test A: MEC -> NEOC reroutes through CRC", path(a, "MEC", "NEOC"), ["MEC", "CRC", "NEOC"]),
        ("test A: return path NEOC -> MEC uses CRC", path(a, "NEOC", "MEC"), ["NEOC", "CRC", "MEC"]),
        ("test A: USR -> NEOC reroutes through MEC and CRC", path(a, "USR", "NEOC"), ["USR", "MEC", "CRC", "NEOC"]),
        ("test B: CRC -> NEOC reroutes through FRH (track 1 down)", path(b, "CRC", "NEOC"), ["CRC", "FRH", "NEOC"]),
        ("test B: MOP -> NEOC reroutes CRC, FRH", path(b, "MOP", "NEOC"), ["MOP", "CRC", "FRH", "NEOC"]),
    ]
    return [Check(name, got == want, " -> ".join(got)) for name, got, want in expect]


def check_track_behaviour(d: Design) -> list[Check]:
    up = Network(d.routers, d.links)
    down = Network(d.routers, d.links, SCENARIOS[TEST_B])
    a = Network(d.routers, d.links, SCENARIOS[TEST_A])
    return [Check("IP SLA track 1 is Up normally and in Test A, Down in Test B",
                  up.track_up("Router-CRC", 1) and a.track_up("Router-CRC", 1) and not down.track_up("Router-CRC", 1),
                  f"normal={up.track_up('Router-CRC', 1)}, A={a.track_up('Router-CRC', 1)}, B={down.track_up('Router-CRC', 1)}")]


def check_justifications(d: Design) -> list[Check]:
    out = []
    # 1. NEOC's return-path floating routes (assumption 9 of the report)
    stripped = "\n".join(l for l in d.router_text("Router-NEOC").splitlines()
                         if not (l.startswith("ip route") and l.rstrip().endswith(f" {FLOATING_AD}")))
    s = d.with_router("Router-NEOC", stripped)
    net = Network(s.routers, s.links, SCENARIOS[TEST_A])
    fwd, back = net.trace(LAN_HOSTS["MEC"], LAN_HOSTS["NEOC"]).ok, net.trace(LAN_HOSTS["NEOC"], LAN_HOSTS["MEC"]).ok
    out.append(Check("Justification: without NEOC's return-path floating routes, Test A breaks the reverse direction",
                     fwd and not back, f"MEC->NEOC ok={fwd}, NEOC->MEC ok={back}"))
    # 2. IP SLA tracking (assumption 11): without `track 1`, the primary never withdraws
    untracked = d.router_text("Router-CRC").replace(" track 1", "")
    s = d.with_router("Router-CRC", untracked)
    net = Network(s.routers, s.links, SCENARIOS[TEST_B])
    t = net.trace(LAN_HOSTS["CRC"], LAN_HOSTS["NEOC"])
    out.append(Check("Justification: without IP SLA tracking, CRC keeps its dead primary route in Test B",
                     not t.ok, t.reason or "unexpectedly delivered"))
    # 3. CRC -> MOP LAN route (present in the Packet Tracer capture, absent from the printed report)
    no_mop = "\n".join(l for l in d.router_text("Router-CRC").splitlines() if "18.50.45.128 255.255.255.192" not in l)
    s = d.with_router("Router-CRC", no_mop)
    total, failures = run_flows(Network(s.routers, s.links))
    out.append(Check("Justification: CRC needs its static route to the MOP LAN (without it, MOP is unreachable)",
                     bool(failures), f"{len(failures)} of {total} flows fail without it"))
    return out


def check_patch(d: Design) -> list[Check]:
    p = d.with_patch()
    out = []
    for label in (NORMAL, TEST_A, TEST_B):
        total, failures = run_flows(Network(p.routers, p.links, SCENARIOS[label]))
        out.append(Check(f"Optional NEOC patch, scenario {label[:1] if label != NORMAL else 'normal'}: full reachability",
                         not failures, "; ".join(f"{l}: {r}" for l, r in failures[:3]) or f"{total} flows delivered"))
    base, patched = Network(d.routers, d.links), Network(p.routers, p.links)
    same = all(base.rib["Router-NEOC"].keys() == patched.rib["Router-NEOC"].keys() for _ in [0]) and \
        {k: (v.next_hop, v.interface) for k, v in base.rib["Router-NEOC"].items()} == \
        {k: (v.next_hop, v.interface) for k, v in patched.rib["Router-NEOC"].items()}
    out.append(Check("Optional NEOC patch leaves NEOC's routing table unchanged in normal operation", same, "floating routes stay out"))
    return out


def run_checks(root: Path = ROOT) -> list[Check]:
    d = Design(root)
    checks: list[Check] = []
    for fn in (check_vlsm, check_addresses, check_tables_match_configs, check_gateways, check_dhcp,
               check_static_design, check_reachability, check_expected_paths, check_track_behaviour,
               check_justifications, check_patch):
        checks += fn(d)
    checks.insert(len(checks) - 4, check_dhcp_relay_paths(d))
    return checks


def main() -> int:
    checks = run_checks()
    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f"[{c.mark}] {c.name.ljust(width)}  {c.detail}")
    failed = [c for c in checks if not c.passed and c.level == "error"]
    warned = [c for c in checks if not c.passed and c.level == "warn"]
    print(f"\n{len(checks) - len(failed) - len(warned)} passed, {len(warned)} finding(s), {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
