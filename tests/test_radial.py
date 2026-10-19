"""Radial menu behaviour: the hub dwell that flips Convert ⇄ Tools."""
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from dragshift import radial  # noqa: E402
from dragshift.radial import Item, RadialMenu  # noqa: E402


@pytest.fixture
def menu():
    QApplication.instance() or QApplication([])
    m = RadialMenu(lambda p: {}, lambda p: "")
    m.rings = {"Convert": [Item("PNG"), Item("JPG")], "Tools": [Item("Crop")]}
    m.ring_names = list(m.rings)
    m.ring_idx = 0
    yield m
    m.hub_timer.stop()
    m.deleteLater()


def test_hub_dwell_is_long_enough_to_read():
    assert radial.HUB_DWELL_MS >= 1000


def test_hub_needs_a_petal_visit_first(menu):
    menu._set_hover(-2)
    assert not menu.hub_timer.isActive()


def test_hub_dwell_starts_timer_and_leaving_cancels(menu):
    menu._set_hover(0)
    menu._set_hover(-2)
    assert menu.hub_timer.isActive()
    assert menu.hub_timer.interval() == radial.HUB_DWELL_MS
    menu._set_hover(1)
    assert not menu.hub_timer.isActive()


def test_resting_in_hub_waits_a_full_dwell_between_flips(menu):
    menu._set_hover(0)
    menu._set_hover(-2)
    menu.next_ring()                      # what the timer fires
    assert menu.ring_idx == 1
    assert menu.hover == -2               # still in the hub, so a mouse move can't re-arm instantly
    assert menu.hub_timer.isActive()      # the next flip waits another full dwell
    menu._set_hover(-2)                   # a mouse move inside the hub must not restart the clock
    assert menu.ring_idx == 1


def test_flip_away_from_hub_clears_hover(menu):
    menu.hover = 0
    menu.next_ring()
    assert menu.ring_idx == 1 and menu.hover == -1


# ---------------------------------------------------------------- a key per wheel
@pytest.fixture
def three(menu):
    menu.rings["Actions"] = [Item("Copy", "⧉", key="action:Copy")]
    menu.ring_names = list(menu.rings)
    return menu


def test_hub_dwell_cycles_through_all_three_rings(three):
    three._set_hover(0)
    three._set_hover(-2)
    seen = [three.ring_idx]
    for _ in range(3):
        three.next_ring()
        seen.append(three.ring_idx)
    assert seen == [0, 1, 2, 0]
    assert three.hub_timer.isActive()


def test_another_wheels_key_jumps_straight_to_it(three):
    three.ring_key("Actions")
    assert three.ring_names[three.ring_idx] == "Actions"
    assert three.swap_anim.state() == three.swap_anim.State.Running   # animated like next_ring
    three.ring_key("Tools")
    assert three.ring_names[three.ring_idx] == "Tools"


def test_same_key_again_shows_the_next_ring(three):
    three.ring_key("Convert")
    assert three.ring_names[three.ring_idx] == "Tools"


def test_key_for_a_missing_ring_does_nothing(menu):
    menu.ring_key("Actions")
    assert menu.ring_idx == 0


def test_key_before_the_files_arrive_is_remembered(menu):
    menu.rings, menu.ring_names = {}, []
    menu.show_ring("Tools")
    assert menu.wanted_ring == "Tools"


def test_key_switch_while_resting_in_hub_waits_a_full_dwell(three):
    three._set_hover(0)
    three._set_hover(-2)
    three.ring_key("Actions")
    assert three.hover == -2 and three.hub_timer.isActive()


def test_hub_hint_names_the_next_rings_key(three):
    three.ring_keys = {"Convert": "⇧", "Tools": "⌘", "Actions": "⌃"}
    three.hub_text = "1 KB"
    three.ring_idx = 1
    assert three.hub_lines() == ("1 KB", "⌃ Actions")
    three.ring_idx = 2
    assert three.hub_lines() == ("1 KB", "⇧ Convert")
    three.ring_keys = {}
    assert three.hub_lines() == ("1 KB", "Convert")
    three._t = three._swap = 1.0
    assert not three.grab().isNull()
