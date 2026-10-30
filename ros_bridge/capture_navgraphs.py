"""Capture each fleet adapter's /nav_graphs into site/nav_graphs/<fleet>.json.

Run with system Python and ROS sourced (scripts/capture-navgraphs). Read-only: it only
subscribes. The site model (auspex.site) builds segments from these files; the bridge (M4)
will also report live graphs so a changed map is detected.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from rmf_building_map_msgs.msg import Graph

sys.path.insert(0, str(Path(__file__).resolve().parent))
from convert import graph_to_dict


class Capture(Node):
    def __init__(self) -> None:
        super().__init__("auspex_capture_navgraphs")
        self.graphs: dict[str, Any] = {}
        self.lock = threading.Lock()
        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,  # latched: get the current graphs
        )
        self.create_subscription(Graph, "/nav_graphs", self._on_graph, qos)

    def _on_graph(self, msg: Any) -> None:
        with self.lock:
            self.graphs[msg.name] = msg


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--fleets", default="deliveryRobot,tinyRobot")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--timeout", type=float, default=20.0)
    args = p.parse_args()
    expected = [f for f in args.fleets.split(",") if f]

    rclpy.init()
    node = Capture()
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()
    try:
        end = time.monotonic() + args.timeout
        while time.monotonic() < end:
            with node.lock:
                if all(f in node.graphs for f in expected):
                    break
            time.sleep(0.2)
        with node.lock:
            graphs = dict(node.graphs)
    finally:
        executor.shutdown(timeout_sec=2.0)
        thread.join(timeout=2.0)
        node.destroy_node()
        rclpy.try_shutdown()

    missing = [f for f in expected if f not in graphs]
    if missing:
        print(f"FAIL no /nav_graphs within {args.timeout:.0f} s for: {', '.join(missing)}")
        print("     is the sim up (scripts/check-levers ready)?")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    for fleet in expected:
        data = graph_to_dict(graphs[fleet], now)
        path = args.out / f"{fleet}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        print(f"OK {fleet}: {len(data['vertices'])} vertices, {len(data['lanes'])} lanes -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
