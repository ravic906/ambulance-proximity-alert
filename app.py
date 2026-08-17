from flask import Flask, request, jsonify, render_template
import time, math, random, uuid, urllib.request, json, os

app = Flask(__name__)

ALERT_CORRIDOR_METERS = 150   # how close to the route path counts as "in the way"
LOOKAHEAD_METERS = 800        # how far ahead of the ambulance we still warn drivers
NUM_SIMULATED_DRIVERS = 6

ambulance_state = {
    "active": False,
    "mission_id": None,
    "lat": None,
    "lng": None,
    "destination": None,
    "route": [],          # list of [lat, lng] from OSRM, ordered start -> destination
    "progress_index": 0,  # nearest route point index to ambulance's current position
    "verified_driver": None,
}

real_drivers = {}       # driver_id -> {lat, lng, last_update}
simulated_drivers = {}  # driver_id -> {lat, lng, target_index}


def haversine_meters(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def fetch_osrm_route(start_lat, start_lng, dest_lat, dest_lng):
    """Calls the public OSRM demo server for a real road route. Returns list of [lat,lng]."""
    url = (
        f"https://router.project-osrm.org/route/v1/driving/"
        f"{start_lng},{start_lat};{dest_lng},{dest_lat}?overview=full&geometries=geojson"
    )
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        coords = data["routes"][0]["geometry"]["coordinates"]  # [lng, lat] pairs
        return [[c[1], c[0]] for c in coords]
    except Exception as e:
        # Fallback: straight line with a few interpolated points if OSRM is unreachable
        steps = 20
        return [
            [start_lat + (dest_lat - start_lat) * i / steps,
             start_lng + (dest_lng - start_lng) * i / steps]
            for i in range(steps + 1)
        ]


def nearest_route_index(lat, lng, route):
    if not route:
        return 0
    best_i, best_d = 0, float("inf")
    for i, (rlat, rlng) in enumerate(route):
        d = haversine_meters(lat, lng, rlat, rlng)
        if d < best_d:
            best_d, best_i = d, i
    return best_i


def point_to_segment_meters(plat, plng, alat, alng, blat, blng):
    """Approx distance from point P to segment AB, in meters, using a local flat-earth projection."""
    lat0 = math.radians((alat + blat) / 2)
    mx = 111320 * math.cos(lat0)
    my = 110540

    ax, ay = alng * mx, alat * my
    bx, by = blng * mx, blat * my
    px, py = plng * mx, plat * my

    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)

    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0, min(1, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def route_aware_check(driver_lat, driver_lng):
    """Returns (alert: bool, distance_along_corridor: float or None) for one driver."""
    route = ambulance_state["route"]
    if not ambulance_state["active"] or not route:
        return False, None

    start_i = ambulance_state["progress_index"]
    cumulative = 0
    for i in range(start_i, len(route) - 1):
        alat, alng = route[i]
        blat, blng = route[i + 1]
        seg_len = haversine_meters(alat, alng, blat, blng)
        d = point_to_segment_meters(driver_lat, driver_lng, alat, alng, blat, blng)
        if d <= ALERT_CORRIDOR_METERS:
            return True, round(cumulative)
        cumulative += seg_len
        if cumulative > LOOKAHEAD_METERS:
            break
    return False, None


def step_simulated_drivers():
    """Advance each simulated driver a little along/near the ambulance's route to look alive."""
    route = ambulance_state["route"]
    if not route:
        return
    for did, d in simulated_drivers.items():
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
        jitter_lat = random.uniform(-0.0015, 0.0015)
        jitter_lng = random.uniform(-0.0015, 0.0015)
        did = str(uuid.uuid4())[:8]
        simulated_drivers[did] = {
            "lat": base_lat + jitter_lat,
            "lng": base_lng + jitter_lng,
            "target_index": min(idx + random.randint(3, 10), len(route) - 1),
        }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/ambulance")
def ambulance_page():
    return render_template("ambulance.html")


@app.route("/driver")
def driver_page():
    return render_template("driver.html")


# ---------- Verification ----------
@app.route("/api/ambulance/verify", methods=["POST"])
def verify_driver():
    data = request.get_json()
    name = (data.get("name") or "").strip()
    license_id = (data.get("license_id") or "").strip()
    has_photo = bool(data.get("photo_captured"))

    if not name or not license_id or not has_photo:
        return jsonify({"status": "error", "message": "Name, license ID, and photo capture are all required."}), 400

    ambulance_state["verified_driver"] = {
        "name": name,
        "license_id": license_id,
        "verified_at": time.time(),
    }
    return jsonify({"status": "ok", "verified": True, "driver": ambulance_state["verified_driver"]})


# ---------- Mission control ----------
@app.route("/api/ambulance/start", methods=["POST"])
def start_mission():
    if not ambulance_state["verified_driver"]:
        return jsonify({"status": "error", "message": "Driver not verified yet."}), 403

    data = request.get_json()
    start_lat, start_lng = data.get("start_lat"), data.get("start_lng")
    dest_lat, dest_lng = data.get("dest_lat"), data.get("dest_lng")

    route = fetch_osrm_route(start_lat, start_lng, dest_lat, dest_lng)

    ambulance_state.update({
        "active": True,
        "mission_id": str(int(time.time())),
        "lat": start_lat,
        "lng": start_lng,
        "destination": {"lat": dest_lat, "lng": dest_lng},
        "route": route,
        "progress_index": 0,
    })

    spawn_simulated_drivers()

    return jsonify({"status": "ok", "mission_id": ambulance_state["mission_id"], "route": route})


@app.route("/api/ambulance/stop", methods=["POST"])
def stop_mission():
    ambulance_state.update({
        "active": False, "lat": None, "lng": None, "destination": None,
        "route": [], "progress_index": 0, "mission_id": None,
    })
    simulated_drivers.clear()
    real_drivers.clear()
    return jsonify({"status": "ok"})


@app.route("/api/ambulance/update", methods=["POST"])
def update_location():
    data = request.get_json()
    if not ambulance_state["active"]:
        return jsonify({"status": "no_active_mission"}), 400
    ambulance_state["lat"] = data.get("lat")
    ambulance_state["lng"] = data.get("lng")
    ambulance_state["progress_index"] = nearest_route_index(
        ambulance_state["lat"], ambulance_state["lng"], ambulance_state["route"]
    )
    step_simulated_drivers()
    return jsonify({"status": "ok", "progress_index": ambulance_state["progress_index"]})


# ---------- Driver side ----------
@app.route("/api/driver/check", methods=["POST"])
def check_proximity():
    data = request.get_json()
    driver_id = data.get("driver_id") or str(uuid.uuid4())[:8]
    lat, lng = data.get("lat"), data.get("lng")

    real_drivers[driver_id] = {"lat": lat, "lng": lng, "last_update": time.time()}
    step_simulated_drivers()

    alert, dist_along = route_aware_check(lat, lng)

    others = []
    for did, d in real_drivers.items():
        if did != driver_id:
            others.append({"lat": d["lat"], "lng": d["lng"], "type": "real"})
    for did, d in simulated_drivers.items():
        others.append({"lat": d["lat"], "lng": d["lng"], "type": "simulated"})

    return jsonify({
        "driver_id": driver_id,
        "alert": alert,
        "distance_along_route_m": dist_along,
        "ambulance_active": ambulance_state["active"],
        "ambulance_lat": ambulance_state["lat"],
        "ambulance_lng": ambulance_state["lng"],
        "route": ambulance_state["route"],
        "other_drivers": others,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
