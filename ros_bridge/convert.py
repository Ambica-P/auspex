"""ROS message -> plain JSON-ready dicts. Shared by the capture tool and the bridge (M4).

Nothing past the bridge imports ROS types: the core sees only these dicts, validated by
Pydantic models (auspex.site.navgraph.NavGraph for graphs).
"""

from __future__ import annotations

from typing import Any

# rmf_building_map_msgs/Param.type
_PARAM_FIELD = {1: "value_string", 2: "value_int", 3: "value_float", 4: "value_bool"}


def param_value(p: Any) -> Any:
    field = _PARAM_FIELD.get(int(p.type))
    return getattr(p, field) if field else None


def params_dict(params: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in params:
        value = param_value(p)
        if value is not None:
            out[p.name] = float(value) if isinstance(value, float) else value
    return out


def graph_to_dict(msg: Any, captured_at: str) -> dict[str, Any]:
    """rmf_building_map_msgs/Graph -> the NavGraph capture format."""
    return {
        "fleet": msg.name,
        "captured_at": captured_at,
        "source": "/nav_graphs",
        "vertices": [
            {
                "index": i,
                "name": v.name,
                "x": float(v.x),
                "y": float(v.y),
                "params": params_dict(v.params),
            }
            for i, v in enumerate(msg.vertices)
        ],
        "lanes": [
            {
                "index": i,
                "v1": int(e.v1_idx),
                "v2": int(e.v2_idx),
                "edge_type": int(e.edge_type),
                "params": params_dict(e.params),
            }
            for i, e in enumerate(msg.edges)
        ],
    }


def _stamp(t: Any) -> float:
    return float(t.sec) + float(t.nanosec) * 1e-9


def fleet_state(msg: Any) -> dict[str, Any]:
    """rmf_fleet_msgs/FleetState -> fleet_state payload."""
    return {
        "fleet": msg.name,
        "robots": [
            {
                "name": r.name,
                "model": r.model,
                "task_id": r.task_id,
                "mode": int(r.mode.mode),
                "battery": round(float(r.battery_percent), 2),
                "level": r.location.level_name,
                "x": round(float(r.location.x), 3),
                "y": round(float(r.location.y), 3),
                "yaw": round(float(r.location.yaw), 3),
                "t": _stamp(r.location.t),  # sim time of the position (real-time factor)
                "path_len": len(r.path),
            }
            for r in msg.robots
        ],
    }


def lane_states(msg: Any) -> dict[str, Any]:
    return {"fleet": msg.fleet_name, "closed_lanes": sorted(int(i) for i in msg.closed_lanes)}


def lift_state(msg: Any) -> dict[str, Any]:
    return {
        "lift_name": msg.lift_name,
        "available_floors": list(msg.available_floors),
        "current_floor": msg.current_floor,
        "destination_floor": msg.destination_floor,
        "door_state": int(msg.door_state),
        "motion_state": int(msg.motion_state),
        "available_modes": [int(m) for m in msg.available_modes],
        "current_mode": int(msg.current_mode),
        "session_id": msg.session_id,
        "lift_time": _stamp(msg.lift_time),
    }


def lift_request(msg: Any) -> dict[str, Any]:
    """rmf_lift_msgs/LiftRequest. request_type: 0 end session, 1 AGV mode, 2 human mode."""
    return {
        "lift_name": msg.lift_name,
        "session_id": msg.session_id,
        "request_type": int(msg.request_type),
        "destination_floor": msg.destination_floor,
        "door_state": int(msg.door_state),
        "request_time": _stamp(msg.request_time),
    }


def door_state(msg: Any) -> dict[str, Any]:
    return {
        "door_name": msg.door_name,
        "mode": int(msg.current_mode.value),
        "door_time": _stamp(msg.door_time),
    }


def ingestor_state(msg: Any) -> dict[str, Any]:
    return {
        "guid": msg.guid,
        "mode": int(msg.mode),
        "queue": list(msg.request_guid_queue),
        "seconds_remaining": round(float(msg.seconds_remaining), 2),
    }


def dispatch_states(msg: Any) -> dict[str, Any]:
    def one(d: Any) -> dict[str, Any]:
        return {
            "task_id": d.task_id,
            "status": int(d.status),
            "fleet": d.assignment.fleet_name,
            "robot": d.assignment.expected_robot_name,
            "errors": list(d.errors),
        }

    return {"active": [one(d) for d in msg.active], "finished": [one(d) for d in msg.finished]}
