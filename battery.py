"""Logique batterie et alertes séparée des accès au matériel.

La tension seule ne mesure pas exactement le pourcentage de charge; les libellés
sont des classes indicatives reprises du programme SolarStation existant.
"""
from collections import deque
from dataclasses import dataclass


@dataclass
class EtatBatterie:
    """Dernière estimation publiée par le démon."""
    tension: float
    etat: str
    niveau: str
    tendance: str
    alerte_basse: bool = False


class GestionBatterie:
    """Classe la tension, suit sa tendance et applique l'hystérésis d'alerte."""
    def __init__(self, seuil_bas=12.9, seuil_retour=13.2, fenetre_tendance=6):
        if float(seuil_retour) < float(seuil_bas):
            raise ValueError("seuil_retour doit être supérieur ou égal à seuil_bas")
        self.seuil_bas = float(seuil_bas)
        self.seuil_retour = float(seuil_retour)
        self._historique = deque(maxlen=max(2, int(fenetre_tendance)))
        self.alerte_basse = False

    def analyser(self, tension):
        """Retourne un état arrondi; l'alerte se réarme à 13,2 V inclus."""
        tension = float(tension)
        if not self.alerte_basse and tension < self.seuil_bas:
            self.alerte_basse = True
        elif self.alerte_basse and tension >= self.seuil_retour:
            self.alerte_basse = False

        if tension < 12.60:
            etat, niveau = "Batterie très faible", "tres_faible"
        elif tension < 13.00:
            etat, niveau = "Batterie faible", "faible"
        elif tension < 13.60:
            etat, niveau = "Batterie disponible", "disponible"
        elif tension < 14.00:
            etat, niveau = "Batterie en charge", "charge"
        elif tension < 14.50:
            etat, niveau = "Batterie presque chargée", "presque_chargee"
        else:
            etat, niveau = "Batterie chargée", "chargee"

        tendance = "stable"
        if self._historique:
            delta = tension - self._historique[-1]
            if delta > 0.02:
                tendance = "hausse"
            elif delta < -0.02:
                tendance = "baisse"
        self._historique.append(tension)
        return EtatBatterie(round(tension, 2), etat, niveau, tendance, self.alerte_basse)


class GestionAlertes:
    """Planifie l'alerte initiale, les rappels périodiques et le retour normal.

    L'horloge monotone est fournie par l'appelant pour que les transitions et
    intervalles puissent être testés sans attendre réellement une demi-heure.
    """
    def __init__(self, rappel_minutes=30):
        self.rappel = max(0.0, float(rappel_minutes) * 60.0)
        self.etait_bas = False
        self.prochain_rappel = None

    def evaluer(self, tension, seuil_bas, seuil_retour, now):
        """Renvoie ``basse``, ``rappel``, ``retour`` ou ``None``."""
        tension = float(tension)
        bas = tension < float(seuil_bas)
        if bas:
            if not self.etait_bas:
                self.etait_bas = True
                self.prochain_rappel = now + self.rappel
                return "basse"
            if self.prochain_rappel is not None and now >= self.prochain_rappel:
                self.prochain_rappel = now + self.rappel
                return "rappel"
            return None
        # Le seuil de retour à la normale est inclus (13,2 V par défaut).
        if self.etait_bas and tension >= float(seuil_retour):
            self.etait_bas = False
            self.prochain_rappel = None
            return "retour"
        return None
