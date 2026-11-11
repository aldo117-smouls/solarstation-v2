"""Formatage français des messages MeshCore et consultation Open-Meteo.

Les messages radio restent courts (129 caractères par défaut), limite adaptée
à la messagerie de canal. Cette limite est appliquée aussi à l'outil PC et à
l'outbox afin d'avoir le même comportement pour toutes les voies d'envoi.
"""
from datetime import datetime
import json
import urllib.parse
import urllib.request

MAX_CHARS = 129


def court(texte, limite=MAX_CHARS):
    """Normalise les espaces et tronque sans dépasser la limite demandée."""
    texte = " ".join(str(texte).split())
    if len(texte) <= limite:
        return texte
    return texte[:max(0, limite - 1)].rstrip() + "…"


def tension(volts):
    return court(f"SolarStation | Batterie : {volts:.2f} V")


def info(etat, heure=None):
    if etat is None:
        return court("SolarStation | Infos indisponibles")
    heure = heure or datetime.now().strftime("%d/%m %H:%M")
    return court(f"SolarStation | {etat.tension:.2f} V | {etat.etat} | {etat.tendance} | {heure}")


def alerte(texte):
    return court(f"SolarStation | ALERTE : {texte}")


def aide():
    return court("Commandes: tension, etat, info, ping [secondes], ping stop, aide")


def donnees(etat):
    return tension(etat.tension) if etat else court("SolarStation | Tension indisponible")


def meteo(lat=None, lon=None, ville=None, timeout=8):
    """Récupère la météo courante sans clé via Open-Meteo, par ville ou GPS."""
    if lat is None or lon is None:
        if not ville:
            raise ValueError("Fournir une ville ou latitude/longitude")
        url_geo = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
            {"name": ville, "count": 1, "language": "fr", "format": "json"}
        )
        req = urllib.request.Request(url_geo, headers={"User-Agent": "SolarStation/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            places = json.loads(response.read().decode("utf-8")).get("results", [])
        if not places:
            raise ValueError(f"Ville introuvable: {ville}")
        lat, lon = places[0]["latitude"], places[0]["longitude"]
        ville = places[0].get("name", ville)
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m",
        "timezone": "auto",
    })
    req = urllib.request.Request(url, headers={"User-Agent": "SolarStation/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        current = json.loads(response.read().decode("utf-8")).get("current", {})
    suffix = f" | {ville}" if ville else ""
    return court(
        f"Météo{suffix}: {current['temperature_2m']}°C, humidité "
        f"{current['relative_humidity_2m']}%, vent {current['wind_speed_10m']} km/h"
    )
