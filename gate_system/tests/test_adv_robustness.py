"""Adversarial ROBUSTNESS tests for gate_system.

These prove that malformed-but-valid-JSON config / data files, and malformed
external API responses, crash the system with unhandled exceptions instead of
degrading gracefully. Each test errors/fails against the current code.
"""
import json


from gate import config
from gate.services import proxy


class TestConfigLoadHostileFile:
    """Config.load builds dataclasses via ``CameraConfig(**raw[...])`` with no
    key/type filtering. An unknown key or a wrong-typed section (e.g. produced
    by a newer/older version of the app, or a hand edit) raises TypeError and
    crashes startup instead of falling back to defaults. The module docstring
    promises "Отсутствующие поля заполняются значениями по умолчанию" (graceful
    degradation), which this violates.
    """

    def test_unknown_key_in_section_does_not_crash(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"camera": {"rtsp_url": "rtsp://x", "unknown_key": 1}}),
                     encoding="utf-8")
        # Should degrade gracefully; instead raises TypeError.
        cfg = config.Config.load(p)
        assert cfg.camera.rtsp_url == "rtsp://x"

    def test_wrong_type_section_does_not_crash(self, tmp_path):
        p = tmp_path / "config.json"
        # "camera" is a list, not a mapping -> **list explodes.
        p.write_text(json.dumps({"camera": [1, 2, 3]}), encoding="utf-8")
        cfg = config.Config.load(p)
        assert isinstance(cfg, config.Config)


class TestProxyMalformedApiResponse:
    """ProxyManager.ensure() picks the freshest active proxy using
    ``p.get('unixtime_end', 0)`` (tolerant) but then reads
    ``current['unixtime_end']`` directly (intolerant). An active proxy returned
    by the API without that field raises KeyError. On the startup path
    (main.py line 264: ``proxy_manager.ensure().url``) this is unguarded and
    crashes initialization."""

    def test_ensure_survives_proxy_without_unixtime_end(self, tmp_path):
        class FakeClient:
            def list_proxies(self, descr=None, state="active"):
                return [{
                    "active": "1", "host": "h", "port": 1080,
                    "user": "u", "pass": "p", "type": "socks",
                    # NOTE: no "unixtime_end"
                }]

            def buy(self, *a, **k):
                raise AssertionError("must not attempt a paid buy on malformed data")

        pm = proxy.ProxyManager(
            FakeClient(),
            state_path=str(tmp_path / "state.json"),
            clock=lambda: 1000.0,
        )
        # Should not raise KeyError.
        decision = pm.ensure()
        assert decision is not None
