from pulse.heat import heat_score, heat_visuals


def test_heat_score_weights_and_saturates():
    assert heat_score(0, 0, 30) == 0
    assert heat_score(50, 10, 0) == 100
    assert heat_score(100, 20, -1) == 100
    assert heat_score(25, 5, 15) == 50


def test_visuals_scale_size_glow_color_and_pulse_with_heat():
    cool = heat_visuals(0)
    hot = heat_visuals(100)

    assert cool["node_radius"] == 12
    assert hot["node_radius"] == 48
    assert cool["color"] != hot["color"]
    assert cool["glow"] < hot["glow"]
    assert cool["pulse_seconds"] > hot["pulse_seconds"]
