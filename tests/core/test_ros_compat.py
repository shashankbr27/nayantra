from nayantra.core.adapters import ros_compat as rc


def test_humble_and_jazzy_are_supported(monkeypatch):
    for distro in ("humble", "jazzy"):
        ok, detail = rc.distro_support(distro)
        assert ok and distro.capitalize() in detail


def test_unknown_or_unset_distro_is_reported_not_rejected():
    ok, detail = rc.distro_support("iron")
    assert not ok and "untested" in detail and "Humble" in detail and "Jazzy" in detail
    ok, detail = rc.distro_support("")
    assert not ok and "ROS_DISTRO" in detail


def test_ros_distro_reads_environment(monkeypatch):
    monkeypatch.setenv("ROS_DISTRO", " Jazzy ")
    assert rc.ros_distro() == "jazzy"
    monkeypatch.delenv("ROS_DISTRO")
    assert rc.ros_distro() == ""


def test_cmd_vel_type_follows_the_wire_then_defaults_to_twist():
    assert rc.use_stamped_cmd_vel([]) is False
    assert rc.use_stamped_cmd_vel([rc.TWIST]) is False
    assert rc.use_stamped_cmd_vel([rc.TWIST_STAMPED]) is True
    # mixed endpoints: a Twist consumer exists, so keep Twist
    assert rc.use_stamped_cmd_vel([rc.TWIST, rc.TWIST_STAMPED]) is False


def test_cmd_vel_override_wins():
    assert rc.use_stamped_cmd_vel([rc.TWIST], "true") is True
    assert rc.use_stamped_cmd_vel([rc.TWIST_STAMPED], "false") is False
    assert rc.use_stamped_cmd_vel([rc.TWIST_STAMPED], "  ") is True
