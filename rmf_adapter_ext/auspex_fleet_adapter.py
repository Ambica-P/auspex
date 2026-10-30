"""rmf_demos fleet adapter plus a replan service per robot (spec section 7).

Open-RMF exposes no external way to replan a robot; `RobotUpdateHandle.replan()` exists only
inside the fleet adapter process. This launcher runs the unmodified rmf_demos fleet adapter
(`rmf_demos_fleet_adapter.fleet_adapter.main`, rmf_demos kilted / rmf_fleet_adapter 2.10.1)
and wraps its `ros_connections(node, robots, fleet_handle)` hook to add, on the same node:

    /auspex/replan/<fleet_name>/<robot_name>   std_srvs/srv/Trigger

The service only asks Open-RMF to replan the robot's current go-to-place step; it never
sends motion commands. Run with system Python and ROS sourced (scripts/run-sim does this).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

import rmf_demos_fleet_adapter.fleet_adapter as demo  # type: ignore[import-not-found]
from std_srvs.srv import Trigger  # type: ignore[import-not-found]

SERVICE_PREFIX = "/auspex/replan"


def _require(attr: str) -> Any:
    if not hasattr(demo, attr):
        sys.exit(
            f"auspex_fleet_adapter: rmf_demos_fleet_adapter.fleet_adapter has no '{attr}'. "
            "The rmf_demos version differs from the one this extension was written for "
            "(kilted branch); paste `ros2 pkg xml rmf_demos_fleet_adapter | grep version`."
        )
    return getattr(demo, attr)


_original_ros_connections: Callable[..., list[Any]] = _require("ros_connections")
_demo_main: Callable[[list[str]], Any] = _require("main")


def _replan_callback(node: Any, fleet_name: str, robot_name: str, robot: Any) -> Callable[..., Any]:
    log = node.get_logger()

    def callback(_request: Any, response: Any) -> Any:
        handle = getattr(robot, "update_handle", None)
        if handle is None:
            response.success = False
            response.message = f"robot {robot_name} is not registered with the fleet yet"
            log.warning(f"auspex.replan refused: {response.message}")
            return response
        try:
            handle.more().replan()
        except Exception as exc:  # report every binding failure to the caller
            response.success = False
            response.message = f"replan failed: {type(exc).__name__}: {exc}"
            log.error(f"auspex.replan fleet={fleet_name} robot={robot_name} {response.message}")
            return response
        response.success = True
        response.message = f"replan requested for {fleet_name}/{robot_name}"
        log.info(f"auspex.replan requested fleet={fleet_name} robot={robot_name}")
        return response

    return callback


def ros_connections_with_replan(node: Any, robots: dict[str, Any], fleet_handle: Any) -> list[Any]:
    connections = list(_original_ros_connections(node, robots, fleet_handle))
    fleet_name = fleet_handle.more().fleet_name
    for robot_name, robot in robots.items():
        service = f"{SERVICE_PREFIX}/{fleet_name}/{robot_name}"
        connections.append(
            node.create_service(
                Trigger, service, _replan_callback(node, fleet_name, robot_name, robot)
            )
        )
        node.get_logger().info(f"auspex.replan service ready: {service}")
    return connections


def main(argv: list[str]) -> None:
    demo.ros_connections = ros_connections_with_replan
    _demo_main(argv)


if __name__ == "__main__":
    main(sys.argv)
