import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402

REAL_FETCH_OSRM_ROUTE = app_module.fetch_osrm_route

LAT0 = 17.4
LNG0 = 78.40
M_PER_DEG_LAT = 111195.0                                  # matches haversine's sphere
M_PER_DEG_LNG = 111195.0 * math.cos(math.radians(LAT0))


def point(east_m, north_m=0.0):
    """A [lat, lng] point east_m east and north_m north of (LAT0, LNG0)."""
    return [LAT0 + north_m / M_PER_DEG_LAT, LNG0 + east_m / M_PER_DEG_LNG]


def straight_route(length_m=3000, step_m=50):
    """A straight route heading due east, one point every step_m."""
    return [point(d) for d in range(0, length_m + 1, step_m)]


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    """Every test starts with no mission, no drivers, no operator key, and no network."""
    app_module.ambulance_state.update({
        "active": False, "mission_id": None, "lat": None, "lng": None, "destination": None,
        "route": [], "route_is_fallback": False, "progress_index": 0,
        "verified_driver": None, "token": None,
    })
    app_module.real_drivers.clear()
    app_module.simulated_drivers.clear()
    monkeypatch.setattr(app_module, "_last_sim_step", 0.0)
    monkeypatch.setattr(app_module, "OPERATOR_KEY", "")
    monkeypatch.setattr(app_module, "fetch_osrm_route", lambda *a: (straight_route(), False))
    yield


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def verify(client, **overrides):
    payload = {"name": "Test Driver", "license_id": "DL-1", "photo_captured": True}
    payload.update(overrides)
    return client.post("/api/ambulance/verify", json=payload)


def start_mission(client):
    """Verify and start a mission on the straight test route. Returns the mission token."""
    token = verify(client).get_json()["token"]
    start, dest = straight_route()[0], straight_route()[-1]
    res = client.post("/api/ambulance/start", headers={"X-Mission-Token": token}, json={
        "start_lat": start[0], "start_lng": start[1], "dest_lat": dest[0], "dest_lng": dest[1],
    })
    assert res.status_code == 200, res.get_json()
    return token
