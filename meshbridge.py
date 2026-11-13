"""Adaptateur commandes MeshCore et analyse des événements reçus.

L'API de réception est raccordée dans main.py avec EventType.CHANNEL_MSG_RECV,
MeshCore.subscribe(..., attribute_filters={"channel_idx": index}) puis
start_auto_message_fetching(), comme dans le main.py actuel de l'utilisateur.
"""
import asyncio
import logging
import re
from datetime import datetime

from messages import aide, court, info, meteo as meteo_message, tension

logger = logging.getLogger(__name__)
ALIASES = {"voltage": "tension", "batt": "tension", "batterie": "tension",
           "status": "etat", "infos": "info", "help": "aide", "meteo": "meteo", "météo": "meteo"}


def extraire_texte(event):
    """Lit les formes de payload texte utilisées par les événements MeshCore."""
    payload = getattr(event, "payload", None)
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("text", "message", "msg", "data"):
            if isinstance(payload.get(key), str):
                return payload[key]
    for attr in ("text", "message", "msg"):
        value = getattr(event, attr, None)
        if isinstance(value, str):
            return value
    return ""


def commande_depuis(texte):
    """Normalise ``Nœud: @ping 60`` en paire (commande, argument)."""
    texte = (texte or "").strip()
    if ":" in texte:
        texte = texte.split(":", 1)[1].strip()
    if texte.startswith("@"):
        texte = texte[1:].strip()
    parts = texte.lower().split()
    if not parts:
        return "", None
    cmd = ALIASES.get(parts[0], parts[0])
    arg = " ".join(parts[1:]) or None
    if cmd == "ping" and arg and arg != "stop":
        if len(parts) != 2 or not re.fullmatch(r"\d+", parts[1]):
            return "invalide", None
        return cmd, int(parts[1])
    return cmd, arg


def signalement(event):
    """Ajoute les niveaux SNR/RSSI s'ils existent dans l'événement entrant."""
    payload = getattr(event, "payload", None)
    payload = payload if isinstance(payload, dict) else {}
    values = []
    for keys, label in ((("snr", "rx_snr", "snr_db"), "SNR"), (("rssi", "rx_rssi"), "RSSI")):
        value = next((payload.get(k) for k in keys if payload.get(k) is not None), None)
        if value is None:
            value = next((getattr(event, k, None) for k in keys if getattr(event, k, None) is not None), None)
        if value is not None:
            values.append(f"{label} {value}")
    return " | " + " ".join(values) if values else ""


async def trouver_canal(mesh, nom="MeshCoreStation"):
    """Recherche le nom du canal via la commande get_channel utilisée sur le Pi."""
    for index in range(8):
        try:
            result = await mesh.commands.get_channel(index)
            payload = getattr(result, "payload", None)
            if isinstance(payload, dict) and payload.get("channel_name") == nom:
                return index
        except Exception:
            logger.debug("Canal %s non lisible", index, exc_info=True)
    return None


class MeshBridge:
    """Répond aux requêtes et contrôle la répétition de ping demandée."""
    def __init__(self, mesh, channel, config, get_state, send):
        self.mesh = mesh
        self.channel = channel
        self.config = config
        self.get_state = get_state
        self.send = send
        self.ping_task = None

    async def reply_pong(self, event=None):
        stamp = datetime.now().strftime("%H:%M:%S")
        await self.send(court("Pong " + stamp + (signalement(event) if event else "")))

    async def _repeat_ping(self, interval):
        import time
        deadline = time.monotonic() + float(self.config.get("ping_max_duree_min", 30)) * 60
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(min(interval, max(0, deadline - time.monotonic())))
                if time.monotonic() >= deadline:
                    break
                await self.reply_pong()
        except asyncio.CancelledError:
            raise

    async def _start_ping(self, seconds):
        seconds = max(int(self.config.get("ping_min", 30)), seconds)
        if self.ping_task:
            self.ping_task.cancel()
        await self.reply_pong()
        self.ping_task = asyncio.create_task(self._repeat_ping(seconds))

    async def _reply_meteo(self, ville=None):
        """Interroge Open-Meteo hors de la boucle asynchrone (appel bloquant)."""
        ville = ville or self.config.get("meteo_ville")
        try:
            if ville and ville.lower() == str(self.config.get("meteo_ville", "")).lower():
                lat, lon = self.config.get("meteo_latitude"), self.config.get("meteo_longitude")
            else:
                lat = lon = None
            texte = await asyncio.to_thread(meteo_message, lat, lon, ville)
        except Exception:
            logger.exception("Météo indisponible")
            texte = "SolarStation | Météo indisponible"
        await self.send(texte)

    async def handle_text(self, text, event=None):
        """Exécute uniquement les commandes documentées et ignore les autres."""
        cmd, arg = commande_depuis(text)
        state = self.get_state()
        if cmd == "ping" and arg == "stop":
            if self.ping_task:
                self.ping_task.cancel()
                self.ping_task = None
            await self.send("SolarStation | Ping arrêté")
        elif cmd == "ping" and isinstance(arg, int):
            await self._start_ping(arg)
        elif cmd == "ping":
            await self.reply_pong(event)
        elif cmd == "invalide":
            await self.send("Syntaxe: ping [secondes] ou ping stop")
        elif cmd == "tension":
            await self.send(tension(state.tension) if state else "SolarStation | Tension indisponible")
        elif cmd == "etat":
            await self.send(court(f"SolarStation | {state.tension:.2f} V | {state.etat} | {state.niveau} | {state.tendance}") if state else "SolarStation | Etat indisponible")
        elif cmd == "info":
            await self.send(info(state))
        elif cmd == "meteo":
            await self._reply_meteo(arg)
        elif cmd == "aide":
            await self.send(aide())
        else:
            logger.info("Commande mesh inconnue: %s", cmd)

    async def on_event(self, event):
        """Callback asynchrone passé à ``subscribe`` de la version existante."""
        try:
            await self.handle_text(extraire_texte(event), event)
        except Exception:
            logger.exception("Erreur de traitement commande MeshCore")
