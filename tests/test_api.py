"""HTTP API: verification, mission control, driver checks, privacy and input handling."""
import time

import pytest

import app
from conftest import REAL_FETCH_OSRM_ROUTE, point, start_mission, verify


# ---------- pages ----------
@pytest.mark.parametrize("path", ["/", "/demo", "/ambulance", "/driver"])
def test_pages_load(client, path):
    assert client.get(path).status_code == 200


def test_operator_code_field_shown_only_when_key_set(client, monkeypatch):
    assert b'id="operatorKey"' not in client.get("/ambulance").data
    monkeypatch.setattr(app, "OPERATOR_KEY", "k")
    assert b'id="operatorKey"' in client.get("/ambulance").data


# ---------- verification ----------
def test_verify_returns_token_but_not_license(client):
    data = verify(client).get_json()
    assert data["status"] == "ok" and len(data["token"]) >= 20
    assert "license_id" not in str(data["driver"])


@pytest.mark.parametrize("missing", ["name", "license_id", "photo_captured"])
def test_verify_requires_all_fields(client, missing):
    assert verify(client, **{missing: ""}).status_code == 400


def test_verify_without_body_is_a_400_not_a_crash(client):
    assert client.post("/api/ambulance/verify").status_code == 400


def test_verify_checks_operator_key(client, monkeypatch):
    monkeypatch.setattr(app, "OPERATOR_KEY", "secret")
    assert verify(client).status_code == 403
    assert verify(client, operator_key="wrong").status_code == 403
    assert verify(client, operator_key="secret").status_code == 200


def test_cannot_verify_while_another_mission_is_live(client):
    start_mission(client)
    assert verify(client, name="Intruder").status_code == 409


def test_new_verification_replaces_unused_token(client):
    old = verify(client).get_json()["token"]
    verify(client)
    res = client.post("/api/ambulance/start", headers={"X-Mission-Token": old}, json={
        "start_lat": 17.4, "start_lng": 78.4, "dest_lat": 17.41, "dest_lng": 78.41})
    assert res.status_code == 403


# ---------- mission control needs the token ----------
@pytest.mark.parametrize("path", ["/api/ambulance/start", "/api/ambulance/update", "/api/ambulance/stop"])
@pytest.mark.parametrize("token", [None, "", "wrong-token"])
def test_mission_calls_need_the_token(client, path, token):
    start_mission(client)
    headers = {"X-Mission-Token": token} if token is not None else {}
    res = client.post(path, headers=headers, json={"lat": 17.4, "lng": 78.4})
    assert res.status_code == 403
    assert app.ambulance_state["active"] is True


def test_mission_calls_rejected_before_any_verification(client):
    for path in ["/api/ambulance/start", "/api/ambulance/update", "/api/ambulance/stop"]:
        assert client.post(path, json={}).status_code == 403


@pytest.mark.parametrize("payload", [
    {},
    {"start_lat": 17.4, "start_lng": 78.4, "dest_lat": 17.41},
    {"start_lat": "north", "start_lng": 78.4, "dest_lat": 17.41, "dest_lng": 78.41},
    {"start_lat": "nan", "start_lng": 78.4, "dest_lat": 17.41, "dest_lng": 78.41},
])
def test_start_rejects_bad_coordinates(client, payload):
    token = verify(client).get_json()["token"]
    res = client.post("/api/ambulance/start", headers={"X-Mission-Token": token}, json=payload)
    assert res.status_code == 400
    assert app.ambulance_state["active"] is False


def test_start_reports_routing_fallback(client, monkeypatch):
    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(app, "fetch_osrm_route", REAL_FETCH_OSRM_ROUTE)
    monkeypatch.setattr(app.urllib.request, "urlopen", boom)
    token = verify(client).get_json()["token"]
    res = client.post("/api/ambulance/start", headers={"X-Mission-Token": token}, json={
        "start_lat": 17.4, "start_lng": 78.4, "dest_lat": 17.5, "dest_lng": 78.5})
    data = res.get_json()
    assert data["route_is_fallback"] is True
    assert data["route"][0] == [17.4, 78.4] and data["route"][-1] == [17.5, 78.5]


def test_update_moves_ambulance_along_route(client):
    token = start_mission(client)
    res = client.post("/api/ambulance/update", headers={"X-Mission-Token": token},
                      json=dict(zip(("lat", "lng"), point(510))))
    assert res.get_json()["progress_index"] == 10


def test_update_rejects_missing_coordinates(client):
    token = start_mission(client)
    res = client.post("/api/ambulance/update", headers={"X-Mission-Token": token}, json={})
    assert res.status_code == 400


def test_stop_ends_mission_and_invalidates_token(client):
    token = start_mission(client)
    assert client.post("/api/ambulance/stop", headers={"X-Mission-Token": token}).status_code == 200
    assert app.ambulance_state["active"] is False
    assert client.post("/api/ambulance/update", headers={"X-Mission-Token": token},
                       json={"lat": 17.4, "lng": 78.4}).status_code == 403
    assert verify(client).status_code == 200


# ---------- driver checks ----------
def check(client, driver_id, east, north=0.0):
    lat, lng = point(east, north)
    return client.post("/api/driver/check", json={"driver_id": driver_id, "lat": lat, "lng": lng}).get_json()


def test_driver_with_no_mission(client):
    data = check(client, "d1", 300)
    assert data["ambulance_active"] is False and data["alert"] is False


def test_driver_ahead_on_route_is_alerted(client):
    start_mission(client)
    data = check(client, "d1", 300)
    assert data["alert"] is True
    assert data["distance_along_route_m"] == pytest.approx(300, abs=5)


def test_driver_off_route_is_not_alerted(client):
    start_mission(client)
    assert check(client, "d1", 300, 400)["alert"] is False


def test_drivers_never_see_each_others_locations(client):
    start_mission(client)
    check(client, "d1", 300, 10)
    data = check(client, "d2", 600)
    d1 = point(300, 10)
    for other in data["other_drivers"]:
        assert other["type"] == "simulated"
        assert (other["lat"], other["lng"]) != tuple(d1)


def test_stale_drivers_are_forgotten(client, monkeypatch):
    check(client, "d1", 300)
    assert "d1" in app.real_drivers
    later = time.time() + app.DRIVER_TTL_SECONDS + 1
    monkeypatch.setattr(app.time, "time", lambda: later)
    check(client, "d2", 300)
    assert "d1" not in app.real_drivers and "d2" in app.real_drivers


def test_driver_check_rejects_missing_coordinates(client):
    assert client.post("/api/driver/check", json={"driver_id": "d1"}).status_code == 400


def test_driver_check_generates_an_id_when_missing(client):
    lat, lng = point(0)
    data = client.post("/api/driver/check", json={"lat": lat, "lng": lng}).get_json()
    assert data["driver_id"]


def test_simulated_traffic_moves_at_most_once_per_second(client, monkeypatch):
    start_mission(client)
    before = {k: (v["lat"], v["lng"]) for k, v in app.simulated_drivers.items()}
    now = time.time()
    monkeypatch.setattr(app.time, "time", lambda: now)
    monkeypatch.setattr(app, "_last_sim_step", now)
    for _ in range(20):
        check(client, "d1", 0)
    after = {k: (v["lat"], v["lng"]) for k, v in app.simulated_drivers.items()}
    assert before == after


# ---------- malformed input never crashes the server ----------
@pytest.mark.parametrize("path", [
    "/api/ambulance/verify", "/api/ambulance/start", "/api/ambulance/update",
    "/api/ambulance/stop", "/api/driver/check", "/api/demo/state",
])
@pytest.mark.parametrize("body", ["[1, 2]", '"text"', "42", "null", "{not json"])
def test_non_object_json_is_rejected_cleanly(client, path, body):
    res = client.post(path, data=body, content_type="application/json")
    assert res.status_code < 500


@pytest.mark.parametrize("payload", [
    {"name": 123, "license_id": "L", "photo_captured": True},
    {"name": "A", "license_id": ["L"], "photo_captured": True},
    {"name": "A", "license_id": "L", "photo_captured": True, "operator_key": 123},
])
def test_verify_with_wrong_field_types(client, monkeypatch, payload):
    monkeypatch.setattr(app, "OPERATOR_KEY", "secret")
    assert client.post("/api/ambulance/verify", json=payload).status_code in (400, 403)


def test_driver_id_of_any_type(client):
    lat, lng = point(0)
    for driver_id in [123, {"a": 1}, ["x"], "y" * 500]:
        res = client.post("/api/driver/check", json={"driver_id": driver_id, "lat": lat, "lng": lng})
        assert res.status_code == 200
        assert len(res.get_json()["driver_id"]) <= 32


# ---------- demo ----------
def demo_state(client, cars):
    return client.post("/api/demo/state", json={"cars": cars})


def test_demo_route_endpoint(client):
    data = client.get("/api/demo/route").get_json()
    assert len(data["route"]) == len(data["cumulative_m"]) > 10
    assert data["corridor_m"] == app.ALERT_CORRIDOR_METERS
    assert data["lookahead_m"] == app.LOOKAHEAD_METERS


def test_demo_state_alerts_car_ahead_only(client, monkeypatch):
    monkeypatch.setattr(app.time, "time", lambda: app.DEMO_EPOCH)   # ambulance at route start
    route, cum = app.DEMO_ROUTE, app.DEMO_CUM
    ahead_i = next(i for i, c in enumerate(cum) if c > 300)
    far_i = next(i for i, c in enumerate(cum) if c > 2000)
    cars = [{"id": "ahead", "lat": route[ahead_i][0], "lng": route[ahead_i][1]},
            {"id": "far", "lat": route[far_i][0], "lng": route[far_i][1]}]
    by_id = {c["id"]: c for c in demo_state(client, cars).get_json()["cars"]}
    assert by_id["ahead"]["alert"] is True
    assert by_id["ahead"]["distance_along_route_m"] == pytest.approx(cum[ahead_i], abs=5)
    assert by_id["far"]["alert"] is False


def test_demo_state_skips_bad_cars_and_caps_count(client):
    good = {"id": "g", "lat": app.DEMO_ROUTE[0][0], "lng": app.DEMO_ROUTE[0][1]}
    cars = [{"id": "bad", "lat": "x"}, "not a car", None] + [good] * 20
    data = demo_state(client, cars).get_json()
    assert all(c["id"] == "g" for c in data["cars"])
    assert len(data["cars"]) <= app.DEMO_MAX_CARS


@pytest.mark.parametrize("cars", ["abc", 5, {"id": "a"}, None])
def test_demo_state_with_non_list_cars(client, cars):
    res = client.post("/api/demo/state", json={"cars": cars})
    assert res.status_code == 200
    assert res.get_json()["cars"] == []


@pytest.mark.parametrize("bad", [True, False, None, "", [17.4], {"v": 17.4}, "inf", "-inf"])
def test_coordinates_must_be_real_numbers(client, bad):
    lat, lng = point(0)
    res = client.post("/api/driver/check", json={"driver_id": "d", "lat": bad, "lng": lng})
    assert res.status_code == 400


def test_numeric_strings_are_accepted(client):
    lat, lng = point(0)
    res = client.post("/api/driver/check", json={"driver_id": "d", "lat": str(lat), "lng": str(lng)})
    assert res.status_code == 200


# ---------- concurrency ----------
def test_concurrent_drivers_and_mission_changes_never_error():
    """Many drivers checking in while missions start and stop, as under gunicorn threads."""
    import threading

    app.app.config["TESTING"] = True
    errors = []

    def driver(n):
        c = app.app.test_client()
        for k in range(40):
            lat, lng = point(k * 20, n)
            r = c.post("/api/driver/check", json={"driver_id": f"d{n}-{k % 3}", "lat": lat, "lng": lng})
            if r.status_code != 200:
                errors.append(r.status_code)

    def ambulance():
        c = app.app.test_client()
        for _ in range(10):
            start_mission(c)
            token = app.ambulance_state["token"]
            for k in range(5):
                lat, lng = point(k * 100)
                c.post("/api/ambulance/update", headers={"X-Mission-Token": token}, json={"lat": lat, "lng": lng})
            c.post("/api/ambulance/stop", headers={"X-Mission-Token": token})

    threads = [threading.Thread(target=driver, args=(n,)) for n in range(8)]
    threads.append(threading.Thread(target=ambulance))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
