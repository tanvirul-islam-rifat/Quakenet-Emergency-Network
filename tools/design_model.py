"""Parse the router configs and simulate control-plane (RIB) and data-plane (forwarding) behaviour.

Models only what these designs use: connected routes, static routes (next-hop, exit-interface,
floating AD), RIPv2 with passive interfaces and `redistribute static`, longest-prefix match and
administrative-distance route selection, and link-failure scenarios.
"""
from __future__ import annotations

import csv
import ipaddress as ipa
from dataclasses import dataclass, field
from pathlib import Path

AD_CONNECTED, AD_STATIC, AD_RIP = 0, 1, 120
MAX_HOPS = 16


# ----------------------------------------------------------------------------- parsing
@dataclass
class Interface:
    name: str
    address: ipa.IPv4Interface | None = None
    helper: list[ipa.IPv4Address] = field(default_factory=list)
    shutdown: bool = False


@dataclass
class StaticRoute:
    network: ipa.IPv4Network
    target: str                      # next-hop IP (str) or exit-interface name
    ad: int = AD_STATIC
    track: int | None = None         # bound track object (IP SLA reachability), if any

    @property
    def is_next_hop(self) -> bool:
        return self.target[0].isdigit()


@dataclass
class Rip:
    passive: set[str] = field(default_factory=set)
    redistribute_static: bool = False
    redistribute_metric: int = 1
    version: int = 1
    auto_summary: bool = True


@dataclass
class Router:
    name: str = ""
    interfaces: dict[str, Interface] = field(default_factory=dict)
    statics: list[StaticRoute] = field(default_factory=list)
    rip: Rip | None = None
    excluded: list[tuple[ipa.IPv4Address, ipa.IPv4Address]] = field(default_factory=list)
    pools: list[dict] = field(default_factory=list)
    sla: dict[int, tuple[ipa.IPv4Address, str]] = field(default_factory=dict)   # id -> (target, source interface)
    tracks: dict[int, int] = field(default_factory=dict)                         # track id -> sla id


def parse_router(text: str) -> Router:
    r = Router()
    iface = pool = None
    in_rip = False
    sla_id = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("!"):
            continue
        t = line.split()
        if t[0] in ("exit", "end"):
            iface = pool = None
            in_rip = False
            sla_id = None
        elif t[0] == "hostname":
            r.name = t[1]
        elif t[0] == "interface":
            iface = r.interfaces.setdefault(t[1], Interface(t[1]))
            pool, in_rip = None, False
        elif t[:2] == ["router", "rip"]:
            r.rip, in_rip, iface, pool = Rip(), True, None, None
        elif t[:3] == ["ip", "sla", "schedule"]:
            pass
        elif t[:2] == ["ip", "sla"]:
            sla_id = int(t[2])
            iface = pool = None
            in_rip = False
        elif t[0] == "track" and t[2:4] == ["ip", "sla"]:
            r.tracks[int(t[1])] = int(t[4])
        elif sla_id is not None and t[0] == "icmp-echo":
            src = t[t.index("source-interface") + 1] if "source-interface" in t else ""
            r.sla[sla_id] = (ipa.ip_address(t[1]), src)
        elif t[:3] == ["ip", "dhcp", "pool"]:
            pool = {"name": t[3]}
            r.pools.append(pool)
            iface, in_rip = None, False
        elif t[:3] == ["ip", "dhcp", "excluded-address"]:
            r.excluded.append((ipa.ip_address(t[3]), ipa.ip_address(t[4] if len(t) > 4 else t[3])))
        elif t[:2] == ["ip", "route"]:
            net = ipa.ip_network(f"{t[2]}/{t[3]}")
            route = StaticRoute(net, t[4])
            i = 5
            while i < len(t):                      # optional: <distance> and/or `track <n>`
                if t[i] == "track":
                    route.track = int(t[i + 1])
                    i += 2
                else:
                    route.ad = int(t[i])
                    i += 1
            r.statics.append(route)
        elif iface is not None:
            if t[:2] == ["ip", "address"]:
                iface.address = ipa.ip_interface(f"{t[2]}/{t[3]}")
            elif t[:2] == ["ip", "helper-address"]:
                iface.helper.append(ipa.ip_address(t[2]))
            elif t == ["shutdown"]:
                iface.shutdown = True
        elif in_rip and r.rip is not None:
            if t[0] == "version":
                r.rip.version = int(t[1])
            elif t[:2] == ["no", "auto-summary"]:
                r.rip.auto_summary = False
            elif t[0] == "passive-interface":
                r.rip.passive.add(t[1])
            elif t[:2] == ["redistribute", "static"]:
                r.rip.redistribute_static = True
                if "metric" in t:
                    r.rip.redistribute_metric = int(t[t.index("metric") + 1])
        elif pool is not None:
            if t[0] == "network":
                pool["network"] = ipa.ip_network(f"{t[1]}/{t[2]}")
            elif t[0] == "default-router":
                pool["default_router"] = ipa.ip_address(t[1])
            elif t[0] == "dns-server":
                pool["dns_server"] = ipa.ip_address(t[1])
    return r


def load_routers(config_dir: Path) -> dict[str, Router]:
    routers = {}
    for path in sorted(Path(config_dir).glob("*.txt")):
        router = parse_router(path.read_text())
        routers[router.name] = router
    return routers


def load_links(path: Path) -> list[tuple[str, str, str, str]]:
    with open(path, newline="") as fh:
        return [(r["from_device"], r["from_interface"], r["to_device"], r["to_interface"])
                for r in csv.DictReader(fh)]


# ------------------------------------------------------------------------------- model
@dataclass(frozen=True)
class Route:
    prefix: ipa.IPv4Network
    ad: int
    metric: int
    protocol: str
    next_hop: ipa.IPv4Address | None
    interface: str


@dataclass
class Trace:
    ok: bool
    hops: list[str]
    reason: str = ""
    egress: list[str] = field(default_factory=list)      # outgoing interface used at each hop


class Network:
    """Snapshot of the network for a given set of administratively-down interfaces."""

    def __init__(self, routers: dict[str, Router], links: list, down: set[tuple[str, str]] | None = None):
        self.routers = routers
        self.down = set(down or ())
        self._build_segments(links)
        self.ip_owner = {i.address.ip: (rn, i.name)
                         for rn, r in routers.items() for i in r.interfaces.values() if i.address}
        self.rib: dict[str, dict[ipa.IPv4Network, Route]] = {}
        self._build_ribs()

    # -- layer 2 -------------------------------------------------------------------
    def _build_segments(self, links) -> None:
        parent: dict = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            parent[find(a)] = find(b)

        for d1, i1, d2, i2 in links:
            union((d1, i1), (d2, i2))
            for d in (d1, d2):                       # a switch joins everything attached to it
                if d not in self.routers:
                    union((d, "*"), (d1, i1))
        self._find = find
        self.segment_members: dict = {}
        for rn, r in self.routers.items():
            for name in r.interfaces:
                self.segment_members.setdefault(find((rn, name)), []).append((rn, name))

    def segment(self, rn: str, ifname: str):
        return self._find((rn, ifname))

    def is_up(self, rn: str, ifname: str) -> bool:
        iface = self.routers[rn].interfaces.get(ifname)
        if iface is None or iface.address is None or iface.shutdown or (rn, ifname) in self.down:
            return False
        members = self.segment_members.get(self.segment(rn, ifname), [])
        if len(members) == 2:                        # point-to-point: carrier needs both ends up
            for other in members:
                oi = self.routers[other[0]].interfaces[other[1]]
                if oi.shutdown or other in self.down:
                    return False
        return True

    # -- control plane ---------------------------------------------------------------
    def _connected(self, rn: str) -> list[Route]:
        return [Route(i.address.network, AD_CONNECTED, 0, "C", None, i.name)
                for i in self.routers[rn].interfaces.values() if self.is_up(rn, i.name)]

    def _resolve(self, rn: str, target_ip: ipa.IPv4Address, connected: list[Route]) -> Route | None:
        cands = [c for c in connected if target_ip in c.prefix]
        return max(cands, key=lambda c: c.prefix.prefixlen) if cands else None

    def track_up(self, rn: str, track_id: int) -> bool:
        """IP SLA icmp-echo reachability. The probe target must sit on the source interface's own segment
        (true for this design); it is up while both the source interface and the target's interface are up."""
        r = self.routers[rn]
        sla = r.sla.get(r.tracks.get(track_id, -1))
        if sla is None:
            return False
        target, source_if = sla
        owner = self.ip_owner.get(target)
        return bool(owner and self.is_up(rn, source_if) and self.is_up(*owner)
                    and self.segment(*owner) == self.segment(rn, source_if))

    def _static_candidates(self, rn: str, connected: list[Route]) -> list[Route]:
        out = []
        for s in self.routers[rn].statics:
            if s.track is not None and not self.track_up(rn, s.track):
                continue
            if s.is_next_hop:
                nh = ipa.ip_address(s.target)
                via = self._resolve(rn, nh, connected)
                if via:
                    out.append(Route(s.network, s.ad, 0, "S", nh, via.interface))
            elif self.is_up(rn, s.target):
                out.append(Route(s.network, s.ad, 0, "S", None, s.target))
        return out

    @staticmethod
    def _best(routes: list[Route]) -> dict[ipa.IPv4Network, Route]:
        best: dict[ipa.IPv4Network, Route] = {}
        for rt in routes:
            cur = best.get(rt.prefix)
            if cur is None or (rt.ad, rt.metric) < (cur.ad, cur.metric):
                best[rt.prefix] = rt
        return best

    def _build_ribs(self) -> None:
        connected = {rn: self._connected(rn) for rn in self.routers}
        statics = {rn: self._best(self._static_candidates(rn, connected[rn])) for rn in self.routers}
        rip_routers = [rn for rn, r in self.routers.items() if r.rip]
        rip: dict[str, dict[ipa.IPv4Network, Route]] = {rn: {} for rn in rip_routers}

        def rip_ifaces(rn):
            r = self.routers[rn]
            return [n for n in r.interfaces if self.is_up(rn, n) and n not in r.rip.passive]

        changed = True
        while changed:
            changed = False
            for rn in rip_routers:
                r = self.routers[rn]
                for ifname in rip_ifaces(rn):
                    adverts = {c.prefix: 0 for c in connected[rn]}
                    if r.rip.redistribute_static:
                        for prefix in statics[rn]:
                            adverts.setdefault(prefix, r.rip.redistribute_metric)
                    for prefix, rt in rip[rn].items():
                        if rt.interface != ifname:          # split horizon
                            adverts.setdefault(prefix, rt.metric)
                    for orn, oif in self.segment_members.get(self.segment(rn, ifname), []):
                        if orn == rn or orn not in rip or oif not in rip_ifaces(orn):
                            continue
                        local = {c.prefix for c in connected[orn]}
                        nh = self.routers[rn].interfaces[ifname].address.ip
                        for prefix, metric in adverts.items():
                            m = metric + 1
                            if m >= 16 or prefix in local:
                                continue
                            cur = rip[orn].get(prefix)
                            if cur is None or m < cur.metric:
                                rip[orn][prefix] = Route(prefix, AD_RIP, m, "R", nh, oif)
                                changed = True
        for rn in self.routers:
            allr = list(connected[rn]) + list(statics[rn].values()) + list(rip.get(rn, {}).values())
            self.rib[rn] = self._best(allr)

    def lookup(self, rn: str, dst: ipa.IPv4Address) -> Route | None:
        cands = [rt for p, rt in self.rib[rn].items() if dst in p]
        return max(cands, key=lambda rt: rt.prefix.prefixlen) if cands else None

    # -- data plane --------------------------------------------------------------------
    def gateway_of(self, host_ip: str) -> str:
        ip = ipa.ip_address(host_ip)
        for rn, r in self.routers.items():
            for i in r.interfaces.values():
                if i.address and ip in i.address.network:
                    return rn
        raise ValueError(f"no router interface covers {host_ip}")

    def trace_from(self, start: str, dst_ip: str) -> Trace:
        dst = ipa.ip_address(dst_ip)
        here, hops, egress = start, [start], []
        for _ in range(MAX_HOPS):
            rt = self.lookup(here, dst)
            if rt is None:
                return Trace(False, hops, f"no route to {dst} at {here}", egress)
            if rt.protocol == "C":
                return Trace(True, hops, "", egress)
            egress.append(rt.interface)
            seg = self.segment(here, rt.interface)
            if rt.next_hop is not None:
                owner = self.ip_owner.get(rt.next_hop)
                if not owner or not self.is_up(*owner) or self.segment(*owner) != seg:
                    return Trace(False, hops, f"next hop {rt.next_hop} unreachable from {here}", egress)
                here = owner[0]
            else:
                peers = [m for m in self.segment_members[seg] if m[0] != here and self.is_up(*m)]
                if len(peers) != 1:
                    return Trace(False, hops, f"exit interface {rt.interface} on {here} has no unique peer", egress)
                here = peers[0][0]
            hops.append(here)
        return Trace(False, hops, "forwarding loop / hop limit exceeded", egress)

    def trace(self, src_ip: str, dst_ip: str) -> Trace:
        return self.trace_from(self.gateway_of(src_ip), dst_ip)
