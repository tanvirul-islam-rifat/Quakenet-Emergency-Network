import ipaddress as ipa
import unittest
from copy import deepcopy

from tools import validate_design as vd
from tools.design_model import Network, parse_router

DOWN_A = {("Router-NEOC", "GigabitEthernet0/0/2")}
DOWN_B = {("Router-NEOC", "GigabitEthernet0/0/1")}


def modified(design, router, transform):
    """Copy of the design in which one router's config text was transformed."""
    d = deepcopy(design)
    d.routers[router] = parse_router(transform(design.router_text(router)))
    return d


class ParserTests(unittest.TestCase):
    def test_parses_interfaces_routes_rip_and_sla(self):
        r = parse_router("""hostname R1
interface Serial0/1/0
 ip address 10.0.0.1 255.255.255.252
 ip helper-address 10.9.9.9
 shutdown
 exit
ip sla 7
 icmp-echo 10.0.0.2 source-interface Serial0/1/0
 frequency 5
 exit
ip sla schedule 7 life forever start-time now
track 3 ip sla 7 reachability
ip route 10.1.0.0 255.255.0.0 10.0.0.2 track 3
ip route 10.2.0.0 255.255.0.0 Serial0/1/0 48
ip route 10.3.0.0 255.255.0.0 10.0.0.2 48 track 3
router rip
 version 2
 no auto-summary
 passive-interface Serial0/1/0
 redistribute static metric 5
 exit
""")
        i = r.interfaces["Serial0/1/0"]
        self.assertEqual(str(i.address), "10.0.0.1/30")
        self.assertTrue(i.shutdown)
        self.assertEqual([str(h) for h in i.helper], ["10.9.9.9"])
        self.assertEqual([(str(s.network), s.target, s.ad, s.track) for s in r.statics],
                         [("10.1.0.0/16", "10.0.0.2", 1, 3), ("10.2.0.0/16", "Serial0/1/0", 48, None),
                          ("10.3.0.0/16", "10.0.0.2", 48, 3)])
        self.assertEqual(r.sla, {7: (ipa.ip_address("10.0.0.2"), "Serial0/1/0")})
        self.assertEqual(r.tracks, {3: 7})
        self.assertTrue(r.rip.redistribute_static and r.rip.redistribute_metric == 5 and not r.rip.auto_summary)

    def test_repo_configs_load(self):
        d = vd.Design()
        self.assertEqual(sorted(d.routers), [f"Router-{n}" for n in ("CRC", "FRH", "MEC", "MOP", "NEOC", "SMS", "USR")])


class RoutingModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = vd.Design()

    def route(self, net, router, prefix):
        return net.rib[router].get(ipa.ip_network(prefix))

    def test_floating_routes_are_not_installed_while_primary_is_up(self):
        net = Network(self.d.routers, self.d.links)
        rt = self.route(net, "Router-MEC", "18.50.0.0/19")
        self.assertEqual((rt.ad, str(rt.next_hop)), (1, "18.50.45.201"))

    def test_recursive_backup_takes_over_when_fibre_fails_and_returns_afterwards(self):
        net = Network(self.d.routers, self.d.links, DOWN_A)
        rt = self.route(net, "Router-MEC", "18.50.0.0/19")
        self.assertEqual((rt.ad, str(rt.next_hop)), (48, "18.50.45.196"))
        self.assertEqual(self.route(Network(self.d.routers, self.d.links), "Router-MEC", "18.50.0.0/19").ad, 1)

    def test_tracked_route_is_withdrawn_only_when_the_probe_fails(self):
        normal = Network(self.d.routers, self.d.links)
        self.assertTrue(normal.track_up("Router-CRC", 1))
        self.assertEqual(self.route(normal, "Router-CRC", "18.50.0.0/19").ad, 1)
        failed = Network(self.d.routers, self.d.links, DOWN_B)
        self.assertFalse(failed.track_up("Router-CRC", 1))
        rt = self.route(failed, "Router-CRC", "18.50.0.0/19")
        self.assertEqual((rt.ad, str(rt.next_hop)), (48, "18.50.45.194"))

    def test_rip_distributes_statics_to_the_warning_loop(self):
        net = Network(self.d.routers, self.d.links)
        for r in ("Router-SMS", "Router-FRH"):
            for prefix in ("18.50.40.0/23", "18.50.32.0/21", "18.50.45.0/25", "18.50.45.128/26"):
                self.assertEqual(self.route(net, r, prefix).protocol, "R", (r, prefix))

    def test_central_segment_routers_do_not_run_rip(self):
        net = Network(self.d.routers, self.d.links)
        for r in ("Router-MEC", "Router-CRC", "Router-USR", "Router-MOP"):
            self.assertFalse(any(rt.protocol == "R" for rt in net.rib[r].values()), r)

    def test_no_gateway_of_last_resort_anywhere(self):
        net = Network(self.d.routers, self.d.links)
        for r in self.d.routers:
            self.assertIsNone(self.route(net, r, "0.0.0.0/0"), r)

    def test_forward_and_reply_paths_differ_between_frh_and_mec(self):
        net = Network(self.d.routers, self.d.links)
        self.assertEqual(net.trace(vd.LAN_HOSTS["FRH"], vd.LAN_HOSTS["MEC"]).hops,
                         ["Router-FRH", "Router-NEOC", "Router-MEC"])
        self.assertEqual(net.trace(vd.LAN_HOSTS["MEC"], vd.LAN_HOSTS["FRH"]).hops, ["Router-MEC", "Router-FRH"])


class PacketTracerCalibrationTests(unittest.TestCase):
    def test_route_counts_match_the_packet_tracer_captures(self):
        """`show ip route` reports 'N subnets' (prefixes incl. /32 local routes); the model must agree on all 7 routers."""
        d = vd.Design()
        net = Network(d.routers, d.links)
        for row in vd.read_csv(vd.ROOT / "tests" / "data" / "pt_capture_route_counts.csv"):
            rn = row["router"]
            local = sum(1 for i in d.routers[rn].interfaces.values() if net.is_up(rn, i.name))
            self.assertEqual(len(net.rib[rn]) + local, int(row["subnets_in_show_ip_route"]), rn)

    def test_crc_capture_only_matches_with_the_mop_lan_route(self):
        d = vd.Design()
        without = modified(d, "Router-CRC", lambda t: "\n".join(l for l in t.splitlines() if "18.50.45.128 255.255.255.192" not in l))
        net = Network(without.routers, without.links)
        local = sum(1 for i in without.routers["Router-CRC"].interfaces.values() if net.is_up("Router-CRC", i.name))
        self.assertEqual(len(net.rib["Router-CRC"]) + local, 16)      # capture shows 17


class DesignCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = vd.Design()

    def test_all_error_level_checks_pass(self):
        failed = [c for c in vd.run_checks() if not c.passed and c.level == "error"]
        self.assertEqual(failed, [], [(c.name, c.detail) for c in failed])

    def test_exactly_one_finding_and_it_is_test_b(self):
        warn = [c for c in vd.run_checks() if c.level == "warn" and not c.passed]
        self.assertEqual(len(warn), 1)
        self.assertIn("B:", warn[0].name)
        self.assertIn("12 of 112", warn[0].detail)

    def test_test_b_gap_is_specific_to_return_traffic_to_crc_and_mop(self):
        net = Network(self.d.routers, self.d.links, DOWN_B)
        _, failures = vd.run_flows(net)
        self.assertEqual({l.split("->")[0] for l, _ in failures}, {"NEOC", "FRH", "SMS"})
        self.assertEqual({l.split("->")[1] for l, _ in failures}, {"CRC", "MOP", "MAIL-CRC", "MAIL-MOP"})
        # forward direction of the failover works
        self.assertEqual(net.trace(vd.LAN_HOSTS["CRC"], vd.LAN_HOSTS["NEOC"]).hops, ["Router-CRC", "Router-FRH", "Router-NEOC"])

    def test_optional_patch_fixes_test_b_without_changing_normal_operation(self):
        p = self.d.with_patch()
        for down in (set(), DOWN_A, DOWN_B):
            _, failures = vd.run_flows(Network(p.routers, p.links, down))
            self.assertEqual(failures, [], down)
        net = Network(p.routers, p.links, DOWN_B)
        self.assertEqual(net.trace(vd.LAN_HOSTS["NEOC"], vd.LAN_HOSTS["CRC"]).hops, ["Router-NEOC", "Router-MEC", "Router-CRC"])

    # ---- the verifier must actually catch mistakes ----------------------------------
    def test_detects_duplicate_ip(self):
        d = deepcopy(self.d)
        d.devices[1]["ip_address"] = d.devices[0]["ip_address"]
        self.assertFalse(vd.check_addresses(d)[1].passed)

    def test_detects_address_outside_plan(self):
        d = deepcopy(self.d)
        d.devices[0]["ip_address"] = "18.50.200.9"
        self.assertFalse(vd.check_addresses(d)[0].passed)

    def test_detects_wrong_floating_distance(self):
        d = modified(self.d, "Router-MEC", lambda t: t.replace("18.50.45.196 48", "18.50.45.196 50", 1))
        self.assertFalse(vd.check_static_design(d)[0].passed)

    def test_detects_unresolvable_next_hop(self):
        d = modified(self.d, "Router-MOP", lambda t: t.replace("18.50.0.0 255.255.224.0 18.50.45.221", "18.50.0.0 255.255.224.0 18.50.99.9"))
        self.assertFalse(vd.check_static_design(d)[1].passed)

    def test_detects_default_route(self):
        d = modified(self.d, "Router-MOP", lambda t: t + "\nip route 0.0.0.0 0.0.0.0 18.50.45.221\n")
        self.assertFalse(vd.check_static_design(d)[4].passed)

    def test_detects_missing_route_in_normal_operation(self):
        d = modified(self.d, "Router-USR", lambda t: "\n".join(l for l in t.splitlines() if "18.50.44.0 255.255.255.0" not in l))
        self.assertFalse(vd.check_reachability(d)[0].passed)
        _, failures = vd.run_flows(Network(d.routers, d.links))
        self.assertTrue(failures and all(l.startswith("USR->") and "SMS" in l for l, _ in failures), failures)

    def test_detects_missing_dhcp_exclusion(self):
        d = modified(self.d, "Router-NEOC", lambda t: t.replace("ip dhcp excluded-address 18.50.44.1 18.50.44.9\n", ""))
        self.assertFalse(vd.check_dhcp(d)[0].passed)

    def test_detects_helper_pointing_at_wrong_dhcp_source(self):
        d = modified(self.d, "Router-USR", lambda t: t.replace("18.50.40.4", "18.50.45.3"))
        self.assertFalse(vd.check_dhcp(d)[2].passed)

    def test_detects_tracked_route_without_probe(self):
        d = modified(self.d, "Router-CRC", lambda t: t.replace("track 1 ip sla 1 reachability\n", ""))
        self.assertFalse(vd.check_static_design(d)[3].passed)

    def test_missing_crc_route_to_mop_lan_is_caught(self):
        d = modified(self.d, "Router-CRC", lambda t: "\n".join(l for l in t.splitlines() if "18.50.45.128 255.255.255.192" not in l))
        self.assertFalse(vd.check_reachability(d)[0].passed)

    def test_without_tracking_crc_black_holes_traffic_in_test_b(self):
        d = modified(self.d, "Router-CRC", lambda t: t.replace(" track 1", ""))
        t = Network(d.routers, d.links, DOWN_B).trace(vd.LAN_HOSTS["CRC"], vd.LAN_HOSTS["NEOC"])
        self.assertFalse(t.ok)

    def test_forwarding_loop_is_detected(self):
        a = parse_router("hostname A\ninterface G0\n ip address 10.0.0.1 255.255.255.252\n exit\nip route 0.0.0.0 0.0.0.0 10.0.0.2\n")
        b = parse_router("hostname B\ninterface G0\n ip address 10.0.0.2 255.255.255.252\n exit\nip route 0.0.0.0 0.0.0.0 10.0.0.1\n")
        t = Network({"A": a, "B": b}, [("A", "G0", "B", "G0")]).trace_from("A", "8.8.8.8")
        self.assertFalse(t.ok)
        self.assertIn("loop", t.reason)


if __name__ == "__main__":
    unittest.main()
