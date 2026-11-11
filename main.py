#!/usr/bin/env python3
"""Démon SolarStation pour le Raspberry Pi de l'utilisateur.

Utilise les appels MeshCore connus pour fonctionner sur son code :
MeshCore.create_serial(port, baud, debug=True), commands.get_channel(index),
commands.send_chan_msg(index, texte), subscribe(EventType.CHANNEL_MSG_RECV,
callback, attribute_filters=...), puis start_auto_message_fetching().
Les fichiers gardent les noms du dépôt pour faciliter le copier-coller direct.
"""
import argparse
import asyncio
import json
import logging
import os
import signal
import time
import uuid
from datetime import datetime
from pathlib import Path

from battery import GestionAlertes, GestionBatterie
from messages import alerte as fmt_alerte, court, info as fmt_info, meteo as meteo_message, tension as fmt_tension
from meshbridge import MeshBridge, trouver_canal
from sensor import INA219Sensor

logger = logging.getLogger("SolarStation")

# Configuration par défaut : le chemin reflète l'installation du code d'origine.
DEFAULT_CONFIG_PATH = Path("/home/ludo/solarstation/config.json")
DEFAULT_CONFIG = {
    "port_serie": "/dev/ttyACM0", "baudrate": 115200,
    "nom_canal": "MeshCoreStation", "intervalle_envoi_tension": 2 * 60 * 60,
    "intervalle_lecture": 10, "ina219_address": 64, "offset_volts": 0.0,
    "echantillons_rapides": 5, "fenetre_lissage": 5,
    "reprises_i2c": 2, "delai_echantillon": 0.01, "delai_reprise_i2c": 0.05,
    "seuil_bas": 12.9, "seuil_retour": 13.2, "rappel_minutes": 30,
    "ping_min": 30, "ping_max_duree_min": 30,
    "fichier_etat": "/home/ludo/solarstation/data/status.json",
    "fichier_historique": "/home/ludo/solarstation/data/history.jsonl",
    "rotation_octets": 1048576, "dossier_outbox": "/home/ludo/solarstation/data/outbox",
    "reconnexion_secondes": 10, "meteo_latitude": 48.8566,
    "meteo_longitude": 2.3522, "meteo_ville": "Paris", "niveau_log": "INFO",
}


def charger_config(path):
    """Charge la configuration utilisateur et complète les seules clés absentes."""
    with open(path, encoding="utf-8") as f:
        loaded = json.load(f)
    if not isinstance(loaded, dict):
        raise ValueError("La racine de config.json doit être un objet JSON")
    # Fusion non destructive : les anciennes clés (ina219_address, intervalle_lecture,
    # fichier_etat) sont conservées et les nouvelles options reçoivent un défaut.
    return {**DEFAULT_CONFIG, **loaded}


def chemin(config, key, base):
    """Résout un chemin absolu tel quel, ou relatif au dossier de config.json."""
    path = Path(config[key])
    return path if path.is_absolute() else base / path


def sauvegarder_etat(path, state, timestamp=None):
    """Écrit atomiquement l'état courant; un état indisponible est explicite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "timestamp": timestamp or datetime.now().astimezone().isoformat(timespec="seconds"),
        "tension": state.tension if state else None,
        "etat": state.etat if state else "indisponible",
        "niveau": state.niveau if state else None,
        "tendance": state.tendance if state else None,
    }
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def append_history(path, data, rotate_bytes):
    """Ajoute un relevé JSONL et conserve au plus un fichier de rotation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size >= int(rotate_bytes):
        backup = path.with_suffix(path.suffix + ".1")
        backup.unlink(missing_ok=True)
        path.replace(backup)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def create_sensor(config):
    """Construit le capteur en respectant l'adresse et le lissage configurés."""
    return INA219Sensor(
        config.get("ina219_address", 64), config.get("offset_volts", 0.0),
        config.get("echantillons_rapides", 5), config.get("fenetre_lissage", 5),
        config.get("reprises_i2c", 2), config.get("delai_reprise_i2c", 0.05),
        config.get("delai_echantillon", 0.01),
    )


class Daemon:
    """Coordonne capteur, état, alertes, outbox et radio avec arrêt propre."""
    def __init__(self, config, base):
        self.c = config
        self.base = base
        self.state = None
        self.battery = GestionBatterie(config.get("seuil_bas", 12.9), config.get("seuil_retour", 13.2))
        self.alerts = GestionAlertes(config.get("rappel_minutes", 30))
        self.mesh = None
        self.bridge = None
        self.channel = None
        self.event_type = None
        self.stop = asyncio.Event()
        self.state_path = chemin(config, "fichier_etat", base)
        self.history_path = chemin(config, "fichier_historique", base)
        self.outbox = chemin(config, "dossier_outbox", base)

    async def send(self, text):
        """Envoie sur le canal actif; ne perd jamais les demandes d'outbox en cas d'échec."""
        if self.mesh is None or self.channel is None:
            logger.warning("Radio indisponible, message non envoyé: %s", text)
            return False
        text = court(text)
        try:
            # Appel identique à celui observé dans le main.py courant de l'utilisateur.
            result = await self.mesh.commands.send_chan_msg(self.channel, text)
            if self.event_type and getattr(result, "type", None) == getattr(self.event_type, "ERROR", object()):
                logger.error("Erreur envoi MeshCore: %s", getattr(result, "payload", result))
                return False
            logger.info("Message MeshCore envoyé (%d caractères)", len(text))
            return True
        except Exception:
            logger.exception("Échec envoi MeshCore")
            return False

    async def attach_mesh(self):
        """Se connecte avec les mêmes méthodes MeshCore que le code qui marche."""
        try:
            from meshcore import EventType, MeshCore
            self.event_type = EventType
            self.mesh = await MeshCore.create_serial(
                self.c.get("port_serie", "/dev/ttyACM0"),
                int(self.c.get("baudrate", 115200)),
                debug=True,
            )
            self.channel = await trouver_canal(self.mesh, self.c.get("nom_canal", "MeshCoreStation"))
            if self.channel is None:
                raise RuntimeError(f"canal {self.c.get('nom_canal')} introuvable parmi les index 0–7")
            self.bridge = MeshBridge(self.mesh, self.channel, self.c, lambda: self.state, self.send)
            # Signature observée sur le code actuel; subscribe est appelé sans await.
            self.mesh.subscribe(
                EventType.CHANNEL_MSG_RECV, self.bridge.on_event,
                attribute_filters={"channel_idx": self.channel},
            )
            await self.mesh.start_auto_message_fetching()
            logger.info("MeshCore prêt : canal %s (index %s)", self.c.get("nom_canal"), self.channel)
            return True
        except Exception:
            logger.exception("MeshCore indisponible; fonctionnement en mode dégradé")
            await self.detach_mesh()
            return False

    async def detach_mesh(self):
        """Tente d'arrêter la lecture automatique et libère le port série."""
        if self.mesh:
            try:
                stop_fetch = getattr(self.mesh, "stop_auto_message_fetching", None)
                if stop_fetch:
                    result = stop_fetch()
                    if hasattr(result, "__await__"):
                        await result
            except Exception:
                logger.debug("Arrêt lecture automatique MeshCore", exc_info=True)
            try:
                await self.mesh.disconnect()
            except Exception:
                logger.debug("Déconnexion MeshCore", exc_info=True)
        self.mesh = self.channel = self.bridge = None

    async def sensor_loop(self):
        """Lit la tension, consigne état/historique, déclenche les alertes à seuil."""
        sensor = None
        while not self.stop.is_set():
            if sensor is None:
                try:
                    sensor = create_sensor(self.c)
                    logger.info("INA219 opérationnel à l'adresse %s", self.c.get("ina219_address", 64))
                except Exception:
                    logger.exception("INA219 absent; nouvelle tentative au prochain cycle")
            if sensor:
                try:
                    volts = await asyncio.to_thread(sensor.get_voltage)
                    self.state = self.battery.analyser(volts)
                    now = datetime.now().astimezone().isoformat(timespec="seconds")
                    record = {"timestamp": now, "tension": self.state.tension,
                              "etat": self.state.etat, "niveau": self.state.niveau,
                              "tendance": self.state.tendance}
                    sauvegarder_etat(self.state_path, self.state, now)
                    append_history(self.history_path, record, self.c.get("rotation_octets", 1048576))
                    logger.info("Batterie: %.2f V — %s — tendance %s",
                                self.state.tension, self.state.etat, self.state.tendance)
                    kind = self.alerts.evaluer(
                        volts, self.c.get("seuil_bas", 12.9), self.c.get("seuil_retour", 13.2), time.monotonic()
                    )
                    if kind == "basse":
                        await self.send(fmt_alerte(f"Tension basse {volts:.2f} V"))
                    elif kind == "rappel":
                        await self.send(fmt_alerte(f"Rappel tension basse {volts:.2f} V"))
                    elif kind == "retour":
                        await self.send(fmt_alerte(f"Tension normale {volts:.2f} V"))
                except Exception:
                    logger.exception("Erreur de lecture INA219; nouvelle tentative au prochain cycle")
                    try:
                        sensor.close()
                    except Exception:
                        pass
                    sensor = None
            await self._sleep(float(self.c.get("intervalle_lecture", 10)))
        if sensor:
            try:
                sensor.close()
            except Exception:
                logger.debug("Fermeture INA219", exc_info=True)

    async def periodic_voltage_loop(self):
        """Publie la tension toutes les deux heures (intervalle configurable)."""
        interval = max(1.0, float(self.c.get("intervalle_envoi_tension", 2 * 60 * 60)))
        while not self.stop.is_set():
            await self._sleep(interval)
            if self.stop.is_set():
                break
            if self.state is None:
                logger.info("Envoi périodique ignoré : tension pas encore mesurée")
            else:
                await self.send(fmt_tension(self.state.tension))

    async def outbox_loop(self):
        """Traite les demandes --send, en conservant celles non transmises."""
        self.outbox.mkdir(parents=True, exist_ok=True)
        while not self.stop.is_set():
            for item_path in sorted(self.outbox.glob("*.json")):
                try:
                    item = json.loads(item_path.read_text(encoding="utf-8"))
                    kind, arg = item.get("type"), item.get("texte", "")
                    if kind == "message":
                        text = court(arg)
                    elif kind == "info":
                        text = fmt_info(self.state)
                    elif kind == "donnee":
                        text = fmt_tension(self.state.tension) if self.state else "SolarStation | Tension indisponible"
                    elif kind == "alerte":
                        text = fmt_alerte(arg)
                    elif kind == "meteo":
                        text = await asyncio.to_thread(
                            meteo_message, item.get("latitude", self.c.get("meteo_latitude")),
                            item.get("longitude", self.c.get("meteo_longitude")),
                            item.get("ville", self.c.get("meteo_ville")),
                        )
                    else:
                        logger.warning("Type outbox inconnu: %s", kind)
                        item_path.unlink(missing_ok=True)
                        continue
                    if self.mesh and await self.send(text):
                        item_path.unlink(missing_ok=True)
                except Exception:
                    logger.exception("Échec de traitement outbox %s", item_path.name)
            await self._sleep(1)

    async def _sleep(self, seconds):
        """Dort jusqu'à l'échéance ou au signal d'arrêt, sans bloquer l'arrêt."""
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=max(0.0, seconds))
        except asyncio.TimeoutError:
            pass

    async def mesh_loop(self):
        """Tente la connexion initiale et les reconnexions au délai configuré."""
        while not self.stop.is_set():
            if not self.mesh:
                await self.attach_mesh()
            await self._sleep(float(self.c.get("reconnexion_secondes", 10)))
            if self.mesh and getattr(self.mesh, "is_connected", True) is False:
                logger.warning("MeshCore déconnecté; reconnexion")
                await self.detach_mesh()

    async def run(self):
        """Lance les tâches permanentes puis les arrête et ferme la radio."""
        tasks = [asyncio.create_task(coro) for coro in
                 (self.sensor_loop(), self.outbox_loop(), self.mesh_loop(), self.periodic_voltage_loop())]
        try:
            await self.stop.wait()
        finally:
            self.stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.detach_mesh()


def queue_send(config, base, kind, text="", extra=None):
    """Écrit atomiquement une demande CLI dans le dossier surveillé par le démon."""
    outbox = chemin(config, "dossier_outbox", base)
    outbox.mkdir(parents=True, exist_ok=True)
    item = {"type": kind, "texte": text}
    item.update(extra or {})
    target = outbox / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.json"
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, target)
    return target


def main():
    """Interprète les commandes --send ou démarre le service en continu."""
    parser = argparse.ArgumentParser(description="SolarStation sur Raspberry Pi")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.exists()
                                                    else Path(__file__).with_name("config.json")))
    parser.add_argument("--send", nargs="+",
                        help="Outbox: message TEXTE | meteo [VILLE|LAT LON] | info | alerte TEXTE | donnee")
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    config = charger_config(config_path)
    base = config_path.parent

    if args.send:
        kind = args.send[0].lower()
        text = " ".join(args.send[1:])
        if kind not in ("message", "info", "alerte", "donnee", "meteo"):
            parser.error("types possibles : message, meteo, info, alerte, donnee")
        if kind in ("message", "alerte") and not text:
            parser.error("un texte est requis pour message et alerte")
        extra = None
        if kind == "meteo" and len(args.send) == 3:
            try:
                extra = {"latitude": float(args.send[1]), "longitude": float(args.send[2]), "ville": ""}
            except ValueError:
                extra = {"ville": text}
        elif kind == "meteo" and len(args.send) == 2:
            try:
                extra = {"latitude": float(args.send[1]), "longitude": None, "ville": ""}
            except ValueError:
                extra = {"ville": args.send[1], "latitude": None, "longitude": None}
        target = queue_send(config, base, kind, text, extra)
        print(f"Demande déposée dans l’outbox : {target}")
        return 0

    logging.basicConfig(level=getattr(logging, str(config.get("niveau_log", "INFO")).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    daemon = Daemon(config, base)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, daemon.stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        loop.run_until_complete(daemon.run())
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
