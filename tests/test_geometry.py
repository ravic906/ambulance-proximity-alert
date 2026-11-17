"""The route-ahead check and the helpers it depends on."""
import pytest

import app
from conftest import point, straight_route


# ---------- haversine ----------
def test_haversine_zero_for_same_point():
    assert app.haversine_meters(17.4, 78.4, 17.4, 78.4) == 0


def test_haversine_one_degree_latitude():
    assert app.haversine_meters(0, 0, 1, 0) == pytest.approx(111195, rel=1e-3)


def test_haversine_matches_test_grid():
    a, b = point(0), point(1000, 0)
    assert app.haversine_meters(*a, *b) == pytest.approx(1000, rel=1e-3)


# ---------- projection onto a segment ----------
def test_projection_point_on_segment():
    a, b, p = point(0), point(100), point(40)
    d, t = app.project_onto_segment(*p, *a, *b)
    assert d == pytest.approx(0, abs=0.5)
    assert t == pytest.approx(0.4, abs=0.01)


def test_projection_perpendicular_offset():
    a, b, p = point(0), point(100), point(50, 30)
    d, t = app.project_onto_segment(*p, *a, *b)
    assert d == pytest.approx(30, rel=0.02)
    assert t == pytest.approx(0.5, abs=0.01)


@pytest.mark.parametrize("east, expected_t", [(-50, 0.0), (150, 1.0)])
def test_projection_clamps_to_segment_ends(east, expected_t):
    a, b, p = point(0), point(100), point(east)
    d, t = app.project_onto_segment(*p, *a, *b)
    assert t == expected_t
    assert d == pytest.approx(50, rel=0.02)


def test_projection_degenerate_segment():
    a, p = point(0), point(0, 20)
    d, t = app.project_onto_segment(*p, *a, *a)
    assert d == pytest.approx(20, rel=0.02)
    assert t == 0.0


# ---------- route-ahead check ----------
ROUTE = straight_route()


@pytest.mark.parametrize("east, north, expected_dist", [
    (300, 0, 300),      # on the road ahead
    (300, 100, 300),    # beside the road, inside the 150 m corridor
    (0, 0, 0),          # level with the ambulance
    (790, 0, 790),      # near the end of the lookahead
])
def test_alerts_drivers_on_the_road_ahead(east, north, expected_dist):
    alert, dist = app.route_ahead_check(ROUTE, 0, *point(east, north))
    assert alert is True
    assert dist == pytest.approx(expected_dist, abs=5)


@pytest.mark.parametrize("east, north, why", [
    (300, 200, "outside the corridor"),
    (1200, 0, "beyond the lookahead"),
    (-300, 0, "behind the ambulance"),
])
def test_does_not_alert(east, north, why):
    alert, dist = app.route_ahead_check(ROUTE, 0, *point(east, north))
    assert (alert, dist) == (False, None), why


def test_corridor_edge():
    inside = app.route_ahead_check(ROUTE, 0, *point(300, app.ALERT_CORRIDOR_METERS - 5))
    outside = app.route_ahead_check(ROUTE, 0, *point(300, app.ALERT_CORRIDOR_METERS + 5))
    assert inside[0] is True and outside[0] is False


def test_lookahead_is_measured_from_the_ambulance_not_the_route_start():
    # Ambulance 2 km along: a car at 2.3 km is 300 m ahead; a car at 1.7 km is behind.
    start_i = 2000 // 50
    alert, dist = app.route_ahead_check(ROUTE, start_i, *point(2300))
    assert alert and dist == pytest.approx(300, abs=5)
    assert app.route_ahead_check(ROUTE, start_i, *point(1700)) == (False, None)


def test_start_offset_shifts_distance():
    # Ambulance 30 m past route[0]: a car at 300 m is 270 m ahead of it.
    alert, dist = app.route_ahead_check(ROUTE, 0, *point(300), start_offset_m=30)
    assert alert and dist == pytest.approx(270, abs=5)


def test_car_just_behind_ambulance_on_same_segment_is_not_alerted():
    alert, _ = app.route_ahead_check(ROUTE, 0, *point(10), start_offset_m=40)
    assert alert is False


def test_distance_uses_closest_point_not_first_segment_in_corridor():
    # The car is 100 m off the road at 600 m; segments from 500 m on are already
    # within 150 m of it, but the distance should be to its closest point (600 m).
    _, dist = app.route_ahead_check(ROUTE, 0, *point(600, 100))
    assert dist == pytest.approx(600, abs=5)


def test_empty_route():
    assert app.route_ahead_check([], 0, *point(0)) == (False, None)


def test_single_point_route():
    assert app.route_ahead_check([point(0)], 0, *point(0)) == (False, None)


def test_known_limitation_corridor_ignores_direction_of_travel():
    # Documents current behaviour, not the goal. Out 1 km east, back 1 km west on a
    # road 50 m to the north. The ambulance is 200 m out; a car on the return road is
    # within 150 m of the outbound road 300 m ahead, so it is alerted even though it is
    # driving the other way. Matching the driver's heading or route would fix this.
    out = [point(d) for d in range(0, 1001, 50)]
    back = [point(d, 50) for d in range(1000, -1, -50)]
    route = out + back
    alert, dist = app.route_ahead_check(route, 4, *point(500, 50))
    assert alert
    assert dist == pytest.approx(300, abs=10)


# ---------- tracking the ambulance's position on the route ----------
def test_nearest_route_index_on_a_straight_route():
    assert app.nearest_route_index(*point(510), ROUTE, 0) == 10


def test_nearest_route_index_empty_route():
    assert app.nearest_route_index(*point(0), [], 0) == 0


def test_nearest_route_index_does_not_jump_back_on_a_route_that_doubles_back():
    # Return leg 20 m from the outbound leg. Ambulance is on the return leg,
    # level with outbound point 5; a full search would snap it back to index 5.
    out = [point(d) for d in range(0, 1001, 50)]               # indices 0-20
    back = [point(d, 20) for d in range(1000, -1, -50)]        # indices 21-41
    route = out + back
    here = point(250, 20)                                      # back-leg index 36
    assert app.nearest_route_index(*here, route, last_index=34) == 36


def test_nearest_route_index_full_search_when_far_off_the_window():
    # Ambulance reappears 2 km on (e.g. GPS gap): fall back to a full search.
    assert app.nearest_route_index(*point(2500), ROUTE, last_index=0) == 50


# ---------- demo ambulance ----------
def test_demo_ambulance_starts_on_route_and_advances():
    i0, _, lat0, lng0, s0 = app.demo_ambulance_at(app.DEMO_EPOCH)
    i1, _, lat1, lng1, s1 = app.demo_ambulance_at(app.DEMO_EPOCH + 10)
    assert s0 == pytest.approx(0)
    assert s1 == pytest.approx(10 * app.DEMO_SPEED_MPS)
    assert (lat0, lng0) == pytest.approx(tuple(app.DEMO_ROUTE[0]))
    assert i1 >= i0


def test_demo_ambulance_loops():
    lap = app.DEMO_CUM[-1] / app.DEMO_SPEED_MPS
    a = app.demo_ambulance_at(app.DEMO_EPOCH + 5)
    b = app.demo_ambulance_at(app.DEMO_EPOCH + lap + 5)
    assert a[4] == pytest.approx(b[4], abs=0.01)


def test_demo_ambulance_offset_is_within_its_segment():
    for t in range(0, 600, 7):
        i, offset, *_ = app.demo_ambulance_at(app.DEMO_EPOCH + t)
        seg = app.DEMO_CUM[i + 1] - app.DEMO_CUM[i]
        assert 0 <= offset <= seg + 1e-6


def test_demo_route_has_no_loop_back_near_itself():
    # A route that passes near itself makes cars far ahead alert as if close.
    r, cum = app.DEMO_ROUTE, app.DEMO_CUM
    for i in range(0, len(r), 3):
        for j in range(i, len(r), 3):
            if cum[j] - cum[i] > 600:
                assert app.haversine_meters(*r[i], *r[j]) > 200, (i, j)
