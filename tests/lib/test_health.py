from dataclasses import replace

from monitor.health import init_health, is_stalled, should_alert_stall, should_heartbeat

# America/Denver wall-clock instants, MST (UTC-7) in December.
DEC1_0600 = 1796130000
DEC1_0700 = 1796133600
DEC1_0800 = 1796137200
DEC1_2300 = 1796191200
DEC2_0659 = 1796219940
DEC2_0700 = 1796220000
DEC2_0701 = 1796220060
AT_0700 = (7, 0)


def test_init_seeds_both_clocks_so_neither_fires_at_boot() -> None:
    h = init_health(1000)
    assert h.last_success_unix == 1000
    assert h.last_heartbeat_unix == 1000
    assert h.consecutive_failures == 0
    assert h.items_tracked == 0
    assert h.death_alerted is False
    assert is_stalled(h, 1000, 600) is False
    assert should_heartbeat(h, 1000, 86400) is False


def test_is_stalled_is_false_just_under_the_threshold() -> None:
    assert is_stalled(init_health(1000), 1599, 600) is False


def test_is_stalled_is_true_at_exactly_the_threshold() -> None:
    # >= : the boundary instant itself counts as stalled.
    assert is_stalled(init_health(1000), 1600, 600) is True


def test_heartbeat_is_not_due_before_the_interval_elapses() -> None:
    assert should_heartbeat(init_health(1000), 1000 + 86399, 86400) is False


def test_heartbeat_is_due_once_the_interval_elapses() -> None:
    assert should_heartbeat(init_health(1000), 1000 + 86400, 86400) is True


# --- HEARTBEAT_AT ---------------------------------------------------------


def test_at_hour_is_not_due_later_the_same_local_day() -> None:
    # The date check is what stops it firing repeatedly for the rest of the day —
    # note the interval has long since elapsed here and it still must not fire.
    h = replace(init_health(DEC1_0700), last_heartbeat_unix=DEC1_0700)
    assert should_heartbeat(h, DEC1_2300, 86400, AT_0700) is False


def test_at_hour_is_not_due_on_a_new_local_day_before_the_hour() -> None:
    h = replace(init_health(DEC1_0700), last_heartbeat_unix=DEC1_0700)
    assert should_heartbeat(h, DEC2_0659, 86400, AT_0700) is False


def test_at_hour_is_due_at_exactly_the_hour_on_a_new_local_day() -> None:
    h = replace(init_health(DEC1_0700), last_heartbeat_unix=DEC1_0700)
    assert should_heartbeat(h, DEC2_0700, 86400, AT_0700) is True


def test_at_hour_is_due_past_the_hour_on_a_new_local_day() -> None:
    h = replace(init_health(DEC1_0700), last_heartbeat_unix=DEC1_0700)
    assert should_heartbeat(h, DEC2_0701, 86400, AT_0700) is True


def test_at_hour_ignores_the_interval_entirely() -> None:
    # A one-second interval must not drag the heartbeat off its hour.
    h = replace(init_health(DEC1_0700), last_heartbeat_unix=DEC1_0700)
    assert should_heartbeat(h, DEC1_0800, 1, AT_0700) is False


def test_a_process_starting_an_hour_early_waits_until_the_next_day() -> None:
    # The documented worst case: seeded lastHeartbeat is already today, so the
    # first heartbeat lands ~25 hours later. That is the intended trade for a
    # predictable hour.
    h = init_health(DEC1_0600)
    assert should_heartbeat(h, DEC1_0700, 86400, AT_0700) is False
    assert should_heartbeat(h, DEC2_0700, 86400, AT_0700) is True
    assert (DEC2_0700 - DEC1_0600) / 3600 == 25


def test_a_backwards_clock_step_does_not_fire_a_heartbeat() -> None:
    # "The local date has changed" means advanced. An NTP correction that steps
    # the clock back a day must not be read as a new day.
    h = replace(init_health(DEC2_0700), last_heartbeat_unix=DEC2_0700)
    assert should_heartbeat(h, DEC1_0800, 86400, AT_0700) is False


def test_at_hour_is_due_even_when_the_interval_has_not_elapsed() -> None:
    # The interval is not merely overridden, it is unread: with heartbeat_at set,
    # the only conditions are "local date advanced" and "at or past the hour". An
    # implementation that AND-ed interval_sec into this branch would pass every
    # other test in this file, because they all happen to have elapsed time and
    # satisfied interval agree in sign.
    h = replace(init_health(DEC1_0700), last_heartbeat_unix=DEC1_0700)
    assert should_heartbeat(h, DEC2_0700, 86400 * 365, AT_0700) is True


# --- BUSY_STALL_ALERT_SEC -------------------------------------------------


def test_a_busy_only_absence_uses_the_longer_threshold() -> None:
    # A shared-login collision is not a fault, so ten minutes of it must not page
    # anyone — but it is still an absence of data, so it cannot be ignored either.
    h = replace(init_health(1000), busy_only=True)
    assert should_alert_stall(h, 1000 + 600, 600, 3600) is False
    assert should_alert_stall(h, 1000 + 3599, 600, 3600) is False
    assert should_alert_stall(h, 1000 + 3600, 600, 3600) is True


def test_a_real_fault_uses_the_short_threshold_even_after_busy_ticks() -> None:
    # The moment anything other than a busy signal happens, the generous window is
    # gone: this is how a permanently-400ing upstream still surfaces, and how a
    # genuine outage is not hidden behind an hour of grace.
    h = replace(init_health(1000), busy_only=False)
    assert should_alert_stall(h, 1000 + 600, 600, 3600) is True
