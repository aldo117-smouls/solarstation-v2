#!/usr/bin/env python3
"""Outil PC pour envoyer un message via le Heltec USB MeshCore.

Cet outil ouvre lui-même le port série : ne pas l'utiliser simultanément avec
le démon sur le même Heltec. Les appels utilisés sont ceux du main.py utilisateur.
"""
import argparse
import asyncio
import json
import logging
from pathlib import Path

from messages import alerte as format_alerte, court, meteo as format_meteo
from meshbridge import trouver_canal

logger = logging.getLogger("mesh_send")


def config_read(path):
    """Lit les valeurs de canal, port et météo dans config.json."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


async def send_mesh(config, text):
    """Ouvre le Heltec, trouve le canal par nom et envoie une seule trame."""
    from meshcore import EventType, MeshCore
    mesh = await MeshCore.create_serial(config.get("port_serie", "/dev/ttyACM0"),
                                        int(config.get("baudrate", 115200)), debug=True)
    try:
        channel = await trouver_canal(mesh, config.get("nom_canal", "MeshCoreStation"))
        if channel is None:
            raise RuntimeError("Canal MeshCore configuré introuvable")
        result = await mesh.commands.send_chan_msg(channel, court(text))
        if getattr(result, "type", None) == getattr(EventType, "ERROR", object()):
            raise RuntimeError(f"Erreur MeshCore: {getattr(result, 'payload', result)}")
        print("Message envoyé")
    finally:
        try:
            await mesh.disconnect()
        except Exception:
            logger.debug("Déconnexion MeshCore", exc_info=True)


def main():
    parser = argparse.ArgumentParser(description="Envoi MeshCore via le Heltec série")
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    parser.add_argument("--port", help="Port série prioritaire sur config.json")
    subs = parser.add_subparsers(dest="action", required=True)
    for name in ("message", "alerte"):
        subs.add_parser(name).add_argument("texte", nargs="+")
    subs.add_parser("info")
    subs.add_parser("donnee")
    weather = subs.add_parser("meteo")
    weather.add_argument("ville_arg", nargs="?")
    weather.add_argument("--ville")
    weather.add_argument("--lat", type=float)
    weather.add_argument("--lon", type=float)
    args = parser.parse_args()
    config = config_read(args.config)
    if args.port:
        config["port_serie"] = args.port
    if args.action == "message":
        text = court(" ".join(args.texte))
    elif args.action == "alerte":
        text = format_alerte(" ".join(args.texte))
    elif args.action == "info":
        text = "SolarStation | en ligne"
    elif args.action == "donnee":
        text = "SolarStation | Donnée indisponible (exécuter sur le Pi pour la tension)"
    else:
        lat = args.lat if args.lat is not None else config.get("meteo_latitude")
        lon = args.lon if args.lon is not None else config.get("meteo_longitude")
        ville = args.ville or args.ville_arg or config.get("meteo_ville")
        try:
            text = format_meteo(lat, lon, ville)
        except Exception as exc:
            parser.error(f"impossible de récupérer la météo : {exc}")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    asyncio.run(send_mesh(config, text))


if __name__ == "__main__":
    main()
