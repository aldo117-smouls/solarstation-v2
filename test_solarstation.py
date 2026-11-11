"""Tests unitaires SolarStation, sans INA219, I2C, MeshCore ni radio réels."""
import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from battery import GestionAlertes, GestionBatterie
from main import Daemon, charger_config, create_sensor
from meshbridge import commande_depuis
from sensor import INA219Sensor


class FauxCapteur:
    """Faux pilote qui échoue volontairement si le code lit le shunt ou courant."""
    def __init__(self, values):
        self.values = iter(values)

    @property
    def bus_voltage(self):
        return next(self.values)

    @property
    def shunt_voltage(self):
        raise AssertionError("La tension ne doit pas être corrigée par le shunt")

    @property
    def current(self):
        raise AssertionError("Le courant ne doit jamais être consulté")


class TestsSolarStation(unittest.TestCase):
    def test_hysteresis_trend_and_return_at_threshold(self):
        battery = GestionBatterie(12.9, 13.2)
        self.assertFalse(battery.analyser(13.0).alerte_basse)
        self.assertTrue(battery.analyser(12.8).alerte_basse)
        self.assertTrue(battery.analyser(13.1).alerte_basse)
        self.assertFalse(battery.analyser(13.2).alerte_basse)
        self.assertEqual(battery.analyser(13.3).tendance, "hausse")
        self.assertEqual(battery.analyser(13.1).tendance, "baisse")

    def test_alert_immediate_reminder_and_recovery_fake_clock(self):
        alerts, now = GestionAlertes(30), 1000
        self.assertEqual(alerts.evaluer(12.8, 12.9, 13.2, now), "basse")
        self.assertIsNone(alerts.evaluer(12.7, 12.9, 13.2, now + 1799))
        self.assertEqual(alerts.evaluer(12.7, 12.9, 13.2, now + 1800), "rappel")
        self.assertIsNone(alerts.evaluer(13.1, 12.9, 13.2, now + 1801))
        self.assertEqual(alerts.evaluer(13.2, 12.9, 13.2, now + 1802), "retour")
        self.assertEqual(alerts.evaluer(12.8, 12.9, 13.2, now + 1803), "basse")

    def test_ping_parser(self):
        self.assertEqual(commande_depuis("Nœud: @ping"), ("ping", None))
        self.assertEqual(commande_depuis("ping 5"), ("ping", 5))
        self.assertEqual(commande_depuis("ping stop"), ("ping", "stop"))
        self.assertEqual(commande_depuis("ping abc"), ("invalide", None))
        self.assertEqual(commande_depuis("TENSION"), ("tension", None))
        self.assertEqual(commande_depuis("etat"), ("etat", None))

    def test_sensor_median_and_smoothing_bus_voltage_only(self):
        sensor = INA219Sensor(driver=FauxCapteur([12.0, 12.1, 12.1, 12.1, 12.1]),
                              samples=5, window=2, sample_delay=0)
        self.assertAlmostEqual(sensor.get_voltage(), 12.1)

    def test_sensor_i2c_retry(self):
        class Instable:
            def __init__(self):
                self.calls = 0
            @property
            def bus_voltage(self):
                self.calls += 1
                if self.calls == 1:
                    raise OSError("I2C")
                return 13.0
        sensor = INA219Sensor(driver=Instable(), samples=1, window=1, retries=1,
                              retry_delay=0, sample_delay=0)
        self.assertAlmostEqual(sensor.get_voltage(), 13.0)

    def test_old_config_keys_are_preserved(self):
        old = {"ina219_address": 64, "intervalle_lecture": 10,
               "fichier_etat": "/home/ludo/solarstation/data/status.json"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(old), encoding="utf-8")
            result = charger_config(path)
        for key, value in old.items():
            self.assertEqual(result[key], value)
        self.assertEqual(result["port_serie"], "/dev/ttyACM0")
        self.assertEqual(result["intervalle_envoi_tension"], 7200)

    def test_create_sensor_passes_configuration_to_mock(self):
        import main
        captured = {}
        class FauxINA:
            def __init__(self, *args):
                captured["args"] = args
        with patch.object(main, "INA219Sensor", FauxINA):
            create_sensor({"ina219_address": "0x40", "echantillons_rapides": 3,
                           "fenetre_lissage": 4, "reprises_i2c": 1,
                           "delai_reprise_i2c": 0, "delai_echantillon": 0})
        self.assertEqual(captured["args"], ("0x40", 0.0, 3, 4, 1, 0, 0))

    def test_meshcore_api_calls_match_user_main(self):
        calls = []
        event_type = SimpleNamespace(CHANNEL_MSG_RECV=object(), ERROR=object())
        class Commands:
            async def get_channel(self, index):
                calls.append(("get_channel", index))
                return SimpleNamespace(payload={"channel_name": "MeshCoreStation"} if index == 2 else {})
            async def send_chan_msg(self, channel, text):
                calls.append(("send_chan_msg", channel, text))
                return SimpleNamespace(type=None)
        class FauxMesh:
            commands = Commands()
            def subscribe(self, event, callback, attribute_filters=None):
                calls.append(("subscribe", event, attribute_filters, asyncio.iscoroutinefunction(callback)))
            async def start_auto_message_fetching(self):
                calls.append(("start_auto_message_fetching",))
            async def disconnect(self):
                calls.append(("disconnect",))
        class FauxMeshCore:
            @staticmethod
            async def create_serial(port, baud, debug=False):
                calls.append(("create_serial", port, baud, debug))
                return FauxMesh()
        fake_module = types.SimpleNamespace(MeshCore=FauxMeshCore, EventType=event_type)
        config = {"port_serie": "/dev/ttyACM0", "baudrate": 115200,
                  "nom_canal": "MeshCoreStation",
                  "fichier_etat": "state.json", "fichier_historique": "history.jsonl",
                  "dossier_outbox": "outbox"}
        async def exercise():
            with tempfile.TemporaryDirectory() as directory:
                daemon = Daemon(config, Path(directory))
                self.assertTrue(await daemon.attach_mesh())
                self.assertTrue(await daemon.send("test"))
                await daemon.detach_mesh()
        with patch.dict(sys.modules, {"meshcore": fake_module}):
            asyncio.run(exercise())
        self.assertIn(("create_serial", "/dev/ttyACM0", 115200, True), calls)
        self.assertIn(("subscribe", event_type.CHANNEL_MSG_RECV,
                       {"channel_idx": 2}, True), calls)
        self.assertIn(("start_auto_message_fetching",), calls)
        self.assertIn(("send_chan_msg", 2, "test"), calls)

    def test_sensor_rejects_voltage_above_16v(self):
        sensor = INA219Sensor(driver=FauxCapteur([16.2]), samples=1, retries=0, sample_delay=0)
        with self.assertRaises(IOError):
            sensor.get_voltage()


if __name__ == "__main__":
    unittest.main()
