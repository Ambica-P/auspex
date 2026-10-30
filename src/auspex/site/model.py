"""Site model: hospital-authored YAML (site/) joined with the fleets' navigation graphs.

- Segments: every door and lift crossing found in a fleet's graph becomes a segment
  automatically (`segment/door-<name>`, `segment/<lift>`); segments.yaml adds lift
  attributes and overrides, and defines corridor segments as an area on a level.
  Each segment knows its lane indices per fleet (rmf_lanes), never hard-coded.
- Zones: rooms, wards and departments with aliases and named waypoints (per fleet).
- Routes: how porters move between zones, as ordered legs with nominal durations.

Alias resolution here is exact after normalisation (case, spaces, punctuation). Anything
fuzzier is Nemotron's job (M10), and its answer is validated against these zones.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import Field, ValidationError, model_validator

from auspex.core.errors import SiteError
from auspex.core.logging import get_logger
from auspex.core.models import Identifier, StrictModel
from auspex.core.urns import Urn
from auspex.site.navgraph import NavGraph

log = get_logger(__name__)

DEFAULT_SITE_DIR = Path(__file__).resolve().parents[3] / "site"

SegmentKind = Literal["corridor", "lift", "door", "room"]


# --------------------------------------------------------------------------- YAML spec


class LiftAttributes(StrictModel):
    bed_capable: bool
    bariatric_capable: bool
    car_size: Literal["standard", "large"]


class LiftSpec(StrictModel):
    name: str
    exclusive: bool = True
    attributes: LiftAttributes
    vendor_urn: Urn
    product: Identifier
    mutex_group: Identifier | None = None
    robot_wait_s: float = Field(default=20.0, ge=0)  # sim s: car arrives, doors, robot boards
    ride_s: float = Field(default=20.0, ge=0)  # sim s: ride between adjacent floors


class DoorSpec(StrictModel):
    name: str | None = None
    exclusive: bool = False
    mutex_group: Identifier | None = None


class Area(StrictModel):
    level: Identifier
    x: tuple[float, float]
    y: tuple[float, float]

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.x[0] >= self.x[1] or self.y[0] >= self.y[1]:
            raise ValueError("area ranges must be [min, max]")
        return self


class AreaSegmentSpec(StrictModel):
    urn: Urn
    name: str
    kind: Literal["corridor", "room"] = "corridor"
    exclusive: bool = False
    area: Area
    mutex_group: Identifier | None = None


class SegmentsFile(StrictModel):
    door_s: float = Field(default=8.0, ge=0)  # sim s: a robot passing a door (open + close)
    lifts: dict[Identifier, LiftSpec]
    doors: dict[Identifier, DoorSpec] = Field(default_factory=dict)
    areas: list[AreaSegmentSpec] = Field(default_factory=list)


class ZoneSpec(StrictModel):
    urn: Urn
    name: str
    kind: Literal["ward", "department", "room", "bed", "supply_point", "station"]
    level: Identifier
    codes: list[Identifier] = Field(default_factory=list)  # structured location codes
    destination_type: Identifier | None = None  # what the source systems call this place
    aliases: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(default_factory=list)
    waypoints: list[Identifier] = Field(min_length=1)  # named waypoints, best first
    segments: list[Urn] = Field(default_factory=list)
    supplies: list[Identifier] = Field(default_factory=list)  # payloads stocked (supply points)


class ZonesFile(StrictModel):
    zones: list[ZoneSpec]


class Leg(StrictModel):
    segment: Urn | None  # None: walking that claims no tracked segment
    duration_s: float = Field(gt=0)


class Conditions(StrictModel):
    """Job fields that restrict a route, e.g. {"bed_type": ["bariatric"]}."""

    only_for: dict[Literal["transport_mode", "bed_type"], list[str]] = Field(default_factory=dict)
    not_for: dict[Literal["transport_mode", "bed_type"], list[str]] = Field(default_factory=dict)


class RouteSpec(StrictModel):
    id: Identifier
    origin: Urn
    destination: Urn
    legs: list[Leg] = Field(min_length=1)
    conditions: Conditions = Field(default_factory=Conditions)

    @property
    def total_s(self) -> float:
        return sum(leg.duration_s for leg in self.legs)


class RoutesFile(StrictModel):
    pad_s: float = Field(default=30.0, ge=0)  # widen each human claim on both sides
    mode_factors: dict[Identifier, float] = Field(default_factory=dict)
    bed_modes: list[Identifier] = Field(default_factory=lambda: ["bed", "stretcher"])
    # Job status -> seconds until the porter starts the route (statuses absent: no forecast;
    # picked_up starts at status_at), and the confidence of the resulting claims.
    status_lead_s: dict[Identifier, float] = Field(default_factory=dict)
    status_confidence: dict[Identifier, Annotated[float, Field(ge=0, le=1)]] = Field(
        default_factory=dict
    )
    routes: list[RouteSpec]


class FleetSpec(StrictModel):
    vendor_urn: Urn
    product: Identifier
    nominal_speed_mps: float = Field(gt=0)  # linear velocity from the fleet adapter config
    speed_factor: float = Field(default=1.0, gt=0, le=1)  # turns, acceleration, negotiation
    certified_for: list[Identifier] = Field(default_factory=list)


class FleetsFile(StrictModel):
    fleets: dict[Identifier, FleetSpec]


# --------------------------------------------------------------------------- built model


class Segment(StrictModel):
    urn: Urn
    name: str
    kind: SegmentKind
    level: str | None  # None for lifts (they span levels)
    exclusive: bool
    rmf_lanes: dict[str, list[int]]  # fleet -> lane indices to close
    internal_lanes: dict[str, list[int]] = Field(default_factory=dict)  # lift car moves
    mutex_group: Identifier | None = None
    attributes: LiftAttributes | None = None
    vendor_urn: Urn | None = None
    product: Identifier | None = None
    robot_wait_s: float | None = None  # lifts only
    ride_s: float | None = None  # lifts only


class Zone(StrictModel):
    urn: Urn
    name: str
    kind: str
    level: str
    codes: list[str]
    destination_type: str | None
    aliases: list[str]
    waypoints: dict[str, list[str]]  # fleet -> named waypoints present in that fleet's graph
    anchor: tuple[float, float]
    segments: list[Urn]
    supplies: list[str] = Field(default_factory=list)


class RouteChoice(StrictModel):
    route: RouteSpec
    usable: bool
    reasons: list[str]  # why it is excluded (empty when usable)


def normalize_alias(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.casefold().replace("&", "and"))


class Site(StrictModel):
    segments: dict[str, Segment]
    zones: dict[str, Zone]
    routes: list[RouteSpec]
    fleets: dict[str, FleetSpec]
    mode_factors: dict[str, float]
    bed_modes: list[str]
    pad_s: float
    aliases: dict[str, str]  # normalised alias -> zone urn
    codes: dict[str, str]  # structured location code (exact) -> zone urn
    door_s: float
    status_lead_s: dict[str, float]
    status_confidence: dict[str, float]
    lane_segments: dict[str, dict[int, list[str]]]  # fleet -> lane -> segment urns

    def segments_for_lane(self, fleet: str, lane: int) -> list[str]:
        return self.lane_segments.get(fleet, {}).get(lane, [])

    def segment(self, urn: str) -> Segment:
        try:
            return self.segments[urn]
        except KeyError:
            raise SiteError("unknown segment", urn=urn) from None

    def zone(self, urn: str) -> Zone:
        try:
            return self.zones[urn]
        except KeyError:
            raise SiteError("unknown zone", urn=urn) from None

    def resolve_code(self, code: str) -> str | None:
        """A source system's structured location code -> zone urn (exact; None if unknown)."""
        return self.codes.get(code.strip())

    def resolve_alias(self, text: str) -> str | None:
        """Exact match after normalisation; None when unknown (never a guess)."""
        return self.aliases.get(normalize_alias(text))

    def lift_for_crossing(self, lift_name: str) -> Segment:
        return self.segment(f"segment/{lift_name}")

    def candidate_routes(
        self,
        origin: str,
        destination: str,
        transport_mode: str | None = None,
        bed_type: str | None = None,
    ) -> list[RouteChoice]:
        """All routes between two zones, each marked usable or not, with the reasons."""
        job = {"transport_mode": transport_mode, "bed_type": bed_type}
        out: list[RouteChoice] = []
        for route in self.routes:
            if route.origin != origin or route.destination != destination:
                continue
            reasons: list[str] = []
            for field_name, values in route.conditions.only_for.items():
                if job[field_name] not in values:
                    reasons.append(f"route only for {field_name} in {values}")
            for field_name, values in route.conditions.not_for.items():
                if job[field_name] in values:
                    reasons.append(f"route not for {field_name}={job[field_name]}")
            for leg in route.legs:
                if leg.segment is None:
                    continue
                seg = self.segment(leg.segment)
                if seg.attributes is None:
                    continue
                if bed_type == "bariatric" and not seg.attributes.bariatric_capable:
                    reasons.append(f"{seg.urn} is not bariatric-capable")
                if transport_mode in self.bed_modes and not seg.attributes.bed_capable:
                    reasons.append(f"{seg.urn} is not bed-capable")
            out.append(RouteChoice(route=route, usable=not reasons, reasons=reasons))
        return sorted(out, key=lambda c: (not c.usable, c.route.total_s))

    def mode_factor(self, transport_mode: str | None) -> float:
        if transport_mode is None:
            return 1.0
        return self.mode_factors.get(transport_mode, 1.0)


# --------------------------------------------------------------------------- build


def _read(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SiteError("cannot read site file", path=str(path), detail=str(exc)) from None
    if not isinstance(data, dict):
        raise SiteError("site file must be a mapping", path=str(path))
    return data


def _parse[T: StrictModel](model: type[T], path: Path) -> T:
    try:
        return model.model_validate(_read(path))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
        )
        raise SiteError(f"invalid site file: {problems}", path=str(path)) from None


def _crossing_segments(
    graphs: dict[str, NavGraph], spec: SegmentsFile, problems: list[str]
) -> dict[str, Segment]:
    lanes: dict[tuple[str, str], dict[str, list[int]]] = {}
    moves: dict[str, dict[str, list[int]]] = {}
    levels: dict[str, set[str]] = {}
    for fleet, graph in graphs.items():
        for (kind, name), idx in graph.crossings().items():
            if kind == "lift_move":
                moves.setdefault(name, {})[fleet] = idx
                continue
            lanes.setdefault((kind, name), {})[fleet] = idx
            if kind == "door":
                levels.setdefault(name, set()).update(graph.lane_level(graph.lanes[i]) for i in idx)

    out: dict[str, Segment] = {}
    for (kind, name), per_fleet in sorted(lanes.items()):
        if kind == "lift":
            lift = spec.lifts.get(name)
            if lift is None:
                problems.append(f"lift {name} is in the nav graphs but not in segments.yaml")
                continue
            out[f"segment/{name}"] = Segment(
                urn=f"segment/{name}",
                name=lift.name,
                kind="lift",
                level=None,
                exclusive=lift.exclusive,
                rmf_lanes=per_fleet,
                internal_lanes=moves.get(name, {}),
                mutex_group=lift.mutex_group,
                attributes=lift.attributes,
                vendor_urn=lift.vendor_urn,
                product=lift.product,
                robot_wait_s=lift.robot_wait_s,
                ride_s=lift.ride_s,
            )
        else:
            door = spec.doors.get(name, DoorSpec())
            lvl = sorted(levels.get(name, set()))
            out[f"segment/door-{name}"] = Segment(
                urn=f"segment/door-{name}",
                name=door.name or f"Door {name}",
                kind="door",
                level=lvl[0] if len(lvl) == 1 else None,
                exclusive=door.exclusive,
                rmf_lanes=per_fleet,
                mutex_group=door.mutex_group,
            )
    for name in spec.lifts:
        if f"segment/{name}" not in out:
            problems.append(f"segments.yaml lift {name} has no lanes in any fleet's graph")
    for name in spec.doors:
        if f"segment/door-{name}" not in out:
            problems.append(f"segments.yaml door {name} has no lanes in any fleet's graph")
    return out


def _build_zones(
    zone_spec: ZonesFile,
    graphs: dict[str, NavGraph],
    segments: dict[str, Segment],
    problems: list[str],
) -> tuple[dict[str, Zone], dict[str, str], dict[str, str]]:
    zones: dict[str, Zone] = {}
    aliases: dict[str, str] = {}
    codes: dict[str, str] = {}
    for z in zone_spec.zones:
        if z.urn in zones:
            problems.append(f"duplicate zone {z.urn}")
            continue
        present: dict[str, list[str]] = {}
        anchor: tuple[float, float] | None = None
        for fleet, g in graphs.items():
            for wp in z.waypoints:
                v = g.waypoint(wp)
                if v is None:
                    continue
                if v.level != z.level:
                    problems.append(f"{z.urn}: waypoint {wp} is on {v.level}, zone is on {z.level}")
                present.setdefault(fleet, []).append(wp)
                if anchor is None:
                    anchor = (v.x, v.y)
        if anchor is None:
            problems.append(f"{z.urn}: none of {z.waypoints} is in any fleet's graph")
            continue
        for seg in z.segments:
            if seg not in segments:
                problems.append(f"{z.urn}: unknown segment {seg}")
        for alias in [z.name, *z.aliases]:
            key = normalize_alias(alias)
            other = aliases.get(key)
            if other is not None and other != z.urn:
                problems.append(f"alias {alias!r} is used by both {other} and {z.urn}")
            aliases[key] = z.urn
        for code in z.codes:
            if code in codes and codes[code] != z.urn:
                problems.append(f"location code {code!r} is used by both {codes[code]} and {z.urn}")
            codes[code] = z.urn
        zones[z.urn] = Zone(
            urn=z.urn,
            name=z.name,
            kind=z.kind,
            level=z.level,
            codes=z.codes,
            destination_type=z.destination_type,
            aliases=z.aliases,
            waypoints=present,
            anchor=anchor,
            segments=z.segments,
            supplies=z.supplies,
        )

    return zones, aliases, codes


def _check_routes(
    route_spec: RoutesFile,
    zones: dict[str, Zone],
    segments: dict[str, Segment],
    problems: list[str],
) -> None:
    ids: set[str] = set()
    for r in route_spec.routes:
        if r.id in ids:
            problems.append(f"duplicate route id {r.id}")
        ids.add(r.id)
        for end in (r.origin, r.destination):
            if end not in zones:
                problems.append(f"route {r.id}: unknown zone {end}")
        for leg in r.legs:
            if leg.segment is not None and leg.segment not in segments:
                problems.append(f"route {r.id}: unknown segment {leg.segment}")


def _lane_index(segments: dict[str, Segment]) -> dict[str, dict[int, list[str]]]:
    out: dict[str, dict[int, list[str]]] = {}
    for seg in segments.values():
        for lanes in (seg.rmf_lanes, seg.internal_lanes):
            for fleet, idx in lanes.items():
                for i in idx:
                    out.setdefault(fleet, {}).setdefault(i, []).append(seg.urn)
    return out


def build_site(site_dir: Path, graphs: dict[str, NavGraph]) -> Site:
    """Build and validate the site model. Raises SiteError listing every problem found."""
    seg_spec = _parse(SegmentsFile, site_dir / "segments.yaml")
    zone_spec = _parse(ZonesFile, site_dir / "zones.yaml")
    route_spec = _parse(RoutesFile, site_dir / "routes.yaml")
    fleet_spec = _parse(FleetsFile, site_dir / "fleets.yaml")
    problems: list[str] = []

    for fleet in graphs:
        if fleet not in fleet_spec.fleets:
            problems.append(f"fleet {fleet} has a nav graph but is not in fleets.yaml")
    for fleet in fleet_spec.fleets:
        if fleet not in graphs:
            problems.append(f"fleets.yaml fleet {fleet} has no nav graph capture")

    segments = _crossing_segments(graphs, seg_spec, problems)
    for area in seg_spec.areas:
        if area.urn in segments:
            problems.append(f"duplicate segment {area.urn}")
            continue
        per_fleet = {
            fleet: idx
            for fleet, g in graphs.items()
            if (idx := g.lanes_in_area(area.area.level, area.area.x, area.area.y))
        }
        if not per_fleet:
            problems.append(f"{area.urn}: no plain lanes inside its area in any fleet's graph")
            continue
        segments[area.urn] = Segment(
            urn=area.urn,
            name=area.name,
            kind=area.kind,
            level=area.area.level,
            exclusive=area.exclusive,
            rmf_lanes=per_fleet,
            mutex_group=area.mutex_group,
        )

    zones, aliases, codes = _build_zones(zone_spec, graphs, segments, problems)

    _check_routes(route_spec, zones, segments, problems)

    lane_segments = _lane_index(segments)
    for fleet, by_lane in lane_segments.items():
        for lane, urns in sorted(by_lane.items()):
            if len(urns) > 1:
                problems.append(f"{fleet} lane {lane} is in several segments: {urns}")

    if problems:
        raise SiteError(
            f"site model invalid ({len(problems)} problems): " + "; ".join(problems),
            path=str(site_dir),
        )
    site = Site(
        segments=segments,
        zones=zones,
        routes=route_spec.routes,
        fleets=fleet_spec.fleets,
        mode_factors=route_spec.mode_factors,
        bed_modes=route_spec.bed_modes,
        pad_s=route_spec.pad_s,
        aliases=aliases,
        codes=codes,
        door_s=seg_spec.door_s,
        status_lead_s=route_spec.status_lead_s,
        status_confidence=route_spec.status_confidence,
        lane_segments=lane_segments,
    )
    log.info(
        "site.built",
        fleets=sorted(graphs),
        segments=len(segments),
        zones=len(zones),
        routes=len(route_spec.routes),
        aliases=len(aliases),
    )
    return site
