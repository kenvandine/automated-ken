"""Regression coverage for the classic-confinement env-leak fix.

automated-ken-runner is a *classic*-confinement snap, so its own
command-chain (see snap/local/command-chain/python-env.sh) sets
PYTHONHOME/PYTHONPATH on this process so its bundled interpreter can find
its own stdlib. Those (and other SNAP*/LD_* vars describing this snap)
must never be forwarded into a *different* snap's ``snap run`` — doing so
was confirmed to break a target snap's own bundled Python (see
``_strip_own_snap_env`` docstring in runner.py for the full story).
"""

from automated_ken_runner.runner import _strip_own_snap_env


def test_strips_python_interpreter_leak_vars():
    env = {
        "PYTHONHOME": "/snap/automated-ken-runner/123/usr",
        "PYTHONPATH": "/snap/automated-ken-runner/123/lib/python3.12/site-packages",
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/runner",
    }
    result = _strip_own_snap_env(env)
    assert "PYTHONHOME" not in result
    assert "PYTHONPATH" not in result
    assert result["PATH"] == "/usr/bin:/bin"
    assert result["HOME"] == "/home/runner"


def test_strips_snap_prefixed_and_ld_vars():
    env = {
        "SNAP": "/snap/automated-ken-runner/123",
        "SNAP_NAME": "automated-ken-runner",
        "SNAP_REVISION": "123",
        "SNAP_ARCH": "amd64",
        "LD_LIBRARY_PATH": "/snap/automated-ken-runner/123/lib",
        "LD_PRELOAD": "/snap/automated-ken-runner/123/lib/libfoo.so",
        "DISPLAY": ":0",
        "WAYLAND_DISPLAY": "wayland-0",
        "XDG_RUNTIME_DIR": "/run/user/1000",
    }
    result = _strip_own_snap_env(env)
    for leaked in ("SNAP", "SNAP_NAME", "SNAP_REVISION", "SNAP_ARCH", "LD_LIBRARY_PATH", "LD_PRELOAD"):
        assert leaked not in result
    # Graphical-session vars a target GUI snap actually needs must survive.
    assert result["DISPLAY"] == ":0"
    assert result["WAYLAND_DISPLAY"] == "wayland-0"
    assert result["XDG_RUNTIME_DIR"] == "/run/user/1000"


def test_empty_env_is_a_noop():
    assert _strip_own_snap_env({}) == {}
