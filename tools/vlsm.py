"""VLSM allocator: sort networks by host demand (largest first) and carve aligned blocks sequentially."""
from __future__ import annotations

import csv
import ipaddress
import math
from pathlib import Path


def prefix_for_hosts(hosts: int) -> int:
    """Smallest prefix whose block holds `hosts` usable addresses (+ network + broadcast)."""
    if hosts < 1:
        raise ValueError("hosts must be >= 1")
    return 32 - math.ceil(math.log2(hosts + 2))


def allocate(base: str, requirements: list[tuple[str, int]]) -> list[dict]:
    """Return one allocation per requirement, largest demand first (ties keep input order)."""
    network = ipaddress.ip_network(base)
    cursor = int(network.network_address)
    end = int(network.broadcast_address)
    order = sorted(range(len(requirements)), key=lambda i: (-requirements[i][1], i))
    plan = []
    for i in order:
        name, hosts = requirements[i]
        prefix = prefix_for_hosts(hosts)
        size = 2 ** (32 - prefix)
        if cursor % size:                      # align the block to its own size
            cursor += size - cursor % size
        if cursor + size - 1 > end:
            raise ValueError(f"address space exhausted while allocating {name}")
        subnet = ipaddress.ip_network((cursor, prefix))
        plan.append({
            "network": name, "required_hosts": hosts, "subnet": subnet,
            "mask": str(subnet.netmask), "first_usable": subnet.network_address + 1,
            "last_usable": subnet.broadcast_address - 1, "broadcast": subnet.broadcast_address,
            "usable_hosts": size - 2,
        })
        cursor += size
    return plan


def load_requirements(path: Path) -> list[tuple[str, int]]:
    with open(path, newline="") as fh:
        return [(r["network"], int(r["required_hosts"])) for r in csv.DictReader(fh)]


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    plan = allocate("13.34.0.0/16", load_requirements(root / "addressing" / "vlsm_requirements.csv"))
    print(f"{'Network':32}{'Need':>6}  {'Subnet':18}{'Usable range':32}")
    for p in plan:
        print(f"{p['network']:32}{p['required_hosts']:>6}  {str(p['subnet']):18}{p['first_usable']} - {p['last_usable']}")
    last = plan[-1]["broadcast"]
    print(f"\nAddress space used: 13.34.0.0 - {last}; remainder of 13.34.0.0/16 is free for expansion.")


if __name__ == "__main__":
    main()
