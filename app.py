from flask import Flask, request, jsonify, render_template
import time, math, random, uuid, urllib.request, json, os, secrets, threading

app = Flask(__name__)

ALERT_CORRIDOR_METERS = 150   # how close to the route path counts as "in the way"
LOOKAHEAD_METERS = 800        # how far ahead of the ambulance we still warn drivers
NUM_SIMULATED_DRIVERS = 6
DRIVER_TTL_SECONDS = 30       # forget a driver's last position after this much silence
SIM_STEP_SECONDS = 1.0        # simulated traffic moves at most once per second
PROGRESS_WINDOW = 60          # route points searched ahead of the last known position
OFF_ROUTE_METERS = 100        # beyond this, fall back to a full-route search

# Optional shared code an ambulance operator must enter to go live.
# Set OPERATOR_KEY in the environment for any public deployment.
OPERATOR_KEY = os.environ.get("OPERATOR_KEY", "")

DEMO_SPEED_MPS = 50 / 3.6     # simulated ambulance speed in demo mode (50 km/h)
DEMO_MAX_CARS = 12

lock = threading.Lock()

ambulance_state = {
    "active": False,
    "mission_id": None,
    "lat": None,
    "lng": None,
    "destination": None,
    "route": [],          # list of [lat, lng] from OSRM, ordered start -> destination
    "route_is_fallback": False,
    "progress_index": 0,  # nearest route point index to ambulance's current position
    "verified_driver": None,
    "token": None,        # issued at verification; required for every mission call
}

real_drivers = {}       # driver_id -> {lat, lng, last_update}; never sent to other clients
simulated_drivers = {}  # driver_id -> {lat, lng, target_index}
_last_sim_step = 0.0


def haversine_meters(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def fetch_osrm_route(start_lat, start_lng, dest_lat, dest_lng):
    """Calls the public OSRM demo server for a real road route.

    Returns (route, is_fallback). route is a list of [lat, lng]. If OSRM is unreachable the
    route is a straight line, and is_fallback is True so the caller can warn the operator.
    """
    url = (
        f"https://router.project-osrm.org/route/v1/driving/"
        f"{start_lng},{start_lat};{dest_lng},{dest_lat}?overview=full&geometries=geojson"
    )
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        coords = data["routes"][0]["geometry"]["coordinates"]  # [lng, lat] pairs
        return [[c[1], c[0]] for c in coords], False
    except Exception:
        steps = 20
        return [
            [start_lat + (dest_lat - start_lat) * i / steps,
             start_lng + (dest_lng - start_lng) * i / steps]
            for i in range(steps + 1)
        ], True


def _nearest_in(lat, lng, route, lo, hi):
    best_i, best_d = lo, float("inf")
    for i in range(lo, hi):
        d = haversine_meters(lat, lng, route[i][0], route[i][1])
        if d < best_d:
            best_d, best_i = d, i
    return best_i, best_d


def nearest_route_index(lat, lng, route, last_index=0):
    """Nearest route point, searched just around the last known position first.

    Searching the whole route lets the position jump when a route doubles back or crosses
    itself, so look a little behind and PROGRESS_WINDOW points ahead of last_index, and only
    fall back to a full search if the ambulance has clearly left that stretch.
    """
    if not route:
        return 0
    lo = max(0, last_index - 2)
    hi = min(len(route), last_index + PROGRESS_WINDOW)
    i, d = _nearest_in(lat, lng, route, lo, hi)
    if d <= OFF_ROUTE_METERS:
        return i
    return _nearest_in(lat, lng, route, 0, len(route))[0]


def point_to_segment_meters(plat, plng, alat, alng, blat, blng):
    """Approx distance from point P to segment AB, in meters, using a local flat-earth projection."""
    return project_onto_segment(plat, plng, alat, alng, blat, blng)[0]


def project_onto_segment(plat, plng, alat, alng, blat, blng):
    """Returns (distance from P to segment AB in meters, t) where t in [0, 1] is how far along
    AB the closest point lies. Uses a local flat-earth projection."""
    lat0 = math.radians((alat + blat) / 2)
    mx = 111320 * math.cos(lat0)
    my = 110540

    ax, ay = alng * mx, alat * my
    bx, by = blng * mx, blat * my
    px, py = plng * mx, plat * my

    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay), 0.0

    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0, min(1, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy), t


def route_ahead_check(route, start_i, driver_lat, driver_lng, start_offset_m=0.0):
    """Is the driver within the corridor of the road ahead of the ambulance?

    route: [[lat, lng], ...]; start_i: index of the route point at or just behind the ambulance;
    start_offset_m: how far the ambulance already is past route[start_i].
    Returns (alert, distance_along_route_m or None). The distance is measured along the road
    to the point on the route closest to the driver.
    """
    if not route:
        return False, None
    cumulative = -start_offset_m
    best_d, best_along = float("inf"), None
    for i in range(start_i, len(route) - 1):
        alat, alng = route[i]
        blat, blng = route[i + 1]
        seg_len = haversine_meters(alat, alng, blat, blng)
        d, t = project_onto_segment(driver_lat, driver_lng, alat, alng, blat, blng)
        along = cumulative + t * seg_len
        if d < best_d and along <= LOOKAHEAD_METERS:
            best_d, best_along = d, along
        cumulative += seg_len
        if cumulative > LOOKAHEAD_METERS:
            break
    if best_d <= ALERT_CORRIDOR_METERS and best_along >= 0:
        return True, round(best_along)
    return False, None


def route_aware_check(driver_lat, driver_lng):
    """Check one driver against the live mission."""
    if not ambulance_state["active"]:
        return False, None
    return route_ahead_check(
        ambulance_state["route"], ambulance_state["progress_index"], driver_lat, driver_lng
    )


def step_simulated_drivers():
    """Advance simulated traffic along the route, at most once per SIM_STEP_SECONDS.

    Throttled by time so the traffic moves at the same speed however many clients poll.
    """
    global _last_sim_step
    now = time.time()
    if now - _last_sim_step < SIM_STEP_SECONDS:
        return
    _last_sim_step = now
    route = ambulance_state["route"]
    if not route:
        return
    for d in simulated_drivers.values():
        target = route[min(d["target_index"], len(route) - 1)]
        d["lat"] += (target[0] - d["lat"]) * 0.05 + random.uniform(-0.00003, 0.00003)
        d["lng"] += (target[1] - d["lng"]) * 0.05 + random.uniform(-0.00003, 0.00003)
        if haversine_meters(d["lat"], d["lng"], target[0], target[1]) < 30:
            d["target_index"] = min(d["target_index"] + random.randint(3, 8), len(route) - 1)


def spawn_simulated_drivers():
    simulated_drivers.clear()
    route = ambulance_state["route"]
    if not route:
        return
    for _ in range(NUM_SIMULATED_DRIVERS):
        idx = random.randint(0, len(route) - 1)
        base_lat, base_lng = route[idx]
        did = str(uuid.uuid4())[:8]
        simulated_drivers[did] = {
            "lat": base_lat + random.uniform(-0.0015, 0.0015),
            "lng": base_lng + random.uniform(-0.0015, 0.0015),
            "target_index": min(idx + random.randint(3, 10), len(route) - 1),
        }


def expire_stale_drivers():
    cutoff = time.time() - DRIVER_TTL_SECONDS
    for did in [k for k, v in real_drivers.items() if v["last_update"] < cutoff]:
        del real_drivers[did]


def body():
    return request.get_json(silent=True) or {}


def as_float(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def mission_token_ok():
    token = request.headers.get("X-Mission-Token", "")
    expected = ambulance_state["token"]
    return bool(expected) and secrets.compare_digest(token, expected)


# ---------- Pages ----------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/ambulance")
def ambulance_page():
    return render_template("ambulance.html", operator_key_required=bool(OPERATOR_KEY))


@app.route("/driver")
def driver_page():
    return render_template("driver.html")


@app.route("/demo")
def demo_page():
    return render_template("demo.html")


# ---------- Verification ----------
@app.route("/api/ambulance/verify", methods=["POST"])
def verify_driver():
    data = body()
    name = (data.get("name") or "").strip()
    license_id = (data.get("license_id") or "").strip()
    has_photo = bool(data.get("photo_captured"))
    operator_key = data.get("operator_key") or ""

    if not name or not license_id or not has_photo:
        return jsonify({"status": "error", "message": "Name, license ID, and photo capture are all required."}), 400
    if OPERATOR_KEY and not secrets.compare_digest(operator_key, OPERATOR_KEY):
        return jsonify({"status": "error", "message": "Operator access code is incorrect."}), 403

    with lock:
        if ambulance_state["active"]:
            return jsonify({"status": "error", "message": "Another ambulance mission is already live."}), 409
        ambulance_state["verified_driver"] = {
            "name": name,
            "license_id": license_id,
            "verified_at": time.time(),
        }
        ambulance_state["token"] = secrets.token_urlsafe(24)
        return jsonify({
            "status": "ok", "verified": True,
            "driver": {"name": name},
            "token": ambulance_state["token"],
        })


# ---------- Mission control ----------
@app.route("/api/ambulance/start", methods=["POST"])
def start_mission():
    if not mission_token_ok():
        return jsonify({"status": "error", "message": "Not verified for this mission."}), 403

    data = body()
    coords = [as_float(data.get(k)) for k in ("start_lat", "start_lng", "dest_lat", "dest_lng")]
    if None in coords:
        return jsonify({"status": "error", "message": "Start and destination coordinates are required."}), 400
    start_lat, start_lng, dest_lat, dest_lng = coords

    # Network call outside the lock so other requests aren't blocked.
    route, is_fallback = fetch_osrm_route(start_lat, start_lng, dest_lat, dest_lng)

    with lock:
        if not mission_token_ok():
            return jsonify({"status": "error", "message": "Not verified for this mission."}), 403
        ambulance_state.update({
            "active": True,
            "mission_id": str(int(time.time())),
            "lat": start_lat,
            "lng": start_lng,
            "destination": {"lat": dest_lat, "lng": dest_lng},
            "route": route,
            "route_is_fallback": is_fallback,
            "progress_index": 0,
        })
        spawn_simulated_drivers()
        return jsonify({
            "status": "ok", "mission_id": ambulance_state["mission_id"],
            "route": route, "route_is_fallback": is_fallback,
        })


@app.route("/api/ambulance/stop", methods=["POST"])
def stop_mission():
    with lock:
        if not mission_token_ok():
            return jsonify({"status": "error", "message": "Not verified for this mission."}), 403
        ambulance_state.update({
            "active": False, "lat": None, "lng": None, "destination": None,
            "route": [], "route_is_fallback": False, "progress_index": 0, "mission_id": None,
            "verified_driver": None, "token": None,
        })
        simulated_drivers.clear()
        return jsonify({"status": "ok"})


@app.route("/api/ambulance/update", methods=["POST"])
def update_location():
    data = body()
    lat, lng = as_float(data.get("lat")), as_float(data.get("lng"))
    if lat is None or lng is None:
        return jsonify({"status": "error", "message": "lat and lng are required."}), 400
    with lock:
        if not mission_token_ok():
            return jsonify({"status": "error", "message": "Not verified for this mission."}), 403
        if not ambulance_state["active"]:
            return jsonify({"status": "no_active_mission"}), 400
        ambulance_state["lat"] = lat
        ambulance_state["lng"] = lng
        ambulance_state["progress_index"] = nearest_route_index(
            lat, lng, ambulance_state["route"], ambulance_state["progress_index"]
        )
        step_simulated_drivers()
        return jsonify({"status": "ok", "progress_index": ambulance_state["progress_index"]})


# ---------- Driver side ----------
@app.route("/api/driver/check", methods=["POST"])
def check_proximity():
    data = body()
    driver_id = str(data.get("driver_id") or uuid.uuid4().hex[:8])[:32]
    lat, lng = as_float(data.get("lat")), as_float(data.get("lng"))
    if lat is None or lng is None:
        return jsonify({"status": "error", "message": "lat and lng are required."}), 400

    with lock:
        # Kept only so the server can run the check; expires after DRIVER_TTL_SECONDS and is
        # never returned to any other client.
        real_drivers[driver_id] = {"lat": lat, "lng": lng, "last_update": time.time()}
        expire_stale_drivers()
        step_simulated_drivers()

        alert, dist_along = route_aware_check(lat, lng)

        # Only simulated traffic is shown. Real drivers' locations are never shared.
        others = [{"lat": d["lat"], "lng": d["lng"], "type": "simulated"}
                  for d in simulated_drivers.values()]

        return jsonify({
            "driver_id": driver_id,
            "alert": alert,
            "distance_along_route_m": dist_along,
            "ambulance_active": ambulance_state["active"],
            "ambulance_lat": ambulance_state["lat"],
            "ambulance_lng": ambulance_state["lng"],
            "mission_id": ambulance_state["mission_id"],
            "route": ambulance_state["route"],
            "other_drivers": others,
        })


# ---------- One-screen demo (no phones, no GPS) ----------
def _load_demo_route():
    path = os.path.join(app.static_folder, "demo_route.json")
    with open(path) as f:
        data = json.load(f)
    route = data["route"]
    cum = [0.0]
    for (alat, alng), (blat, blng) in zip(route, route[1:]):
        cum.append(cum[-1] + haversine_meters(alat, alng, blat, blng))
    return data, route, cum


DEMO_DATA, DEMO_ROUTE, DEMO_CUM = _load_demo_route()
DEMO_EPOCH = time.time()


def demo_ambulance_at(t):
    """Simulated ambulance position at time t: loops along the demo route at DEMO_SPEED_MPS."""
    total = DEMO_CUM[-1]
    s = ((t - DEMO_EPOCH) * DEMO_SPEED_MPS) % total
    i = 0
    while i < len(DEMO_CUM) - 2 and DEMO_CUM[i + 1] <= s:
        i += 1
    seg = DEMO_CUM[i + 1] - DEMO_CUM[i]
    f = (s - DEMO_CUM[i]) / seg if seg else 0
    (alat, alng), (blat, blng) = DEMO_ROUTE[i], DEMO_ROUTE[i + 1]
    return i, s - DEMO_CUM[i], alat + (blat - alat) * f, alng + (blng - alng) * f, s


@app.route("/api/demo/route")
def demo_route():
    return jsonify({
        "name": DEMO_DATA["name"],
        "route": DEMO_ROUTE,
        "cumulative_m": [round(c, 1) for c in DEMO_CUM],
        "speed_mps": DEMO_SPEED_MPS,
        "corridor_m": ALERT_CORRIDOR_METERS,
        "lookahead_m": LOOKAHEAD_METERS,
    })


@app.route("/api/demo/state", methods=["POST"])
def demo_state():
    """Stateless: the browser sends its cars; the server places the ambulance by the clock
    and runs the same route-ahead check used for live missions."""
    cars = body().get("cars") or []
    i, offset, lat, lng, along = demo_ambulance_at(time.time())
    results = []
    for car in cars[:DEMO_MAX_CARS]:
        clat, clng = as_float(car.get("lat")), as_float(car.get("lng"))
        if clat is None or clng is None:
            continue
        alert, dist = route_ahead_check(DEMO_ROUTE, i, clat, clng, start_offset_m=offset)
        results.append({"id": str(car.get("id", ""))[:32], "alert": alert, "distance_along_route_m": dist})
    return jsonify({
        "ambulance": {"lat": lat, "lng": lng, "along_m": round(along, 1), "index": i},
        "cars": results,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="0.0.0.0" if not debug else "127.0.0.1", port=port, debug=debug)
