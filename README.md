# SolarStation — adaptation directe au Raspberry Pi

Cette version conserve les noms `main.py`, `sensor.py`, `battery.py` et `config.json` pour remplacer les fichiers du dépôt sur le Raspberry Pi de l'utilisateur. Les commentaires et messages sont en français.

## Valeurs reprises du code existant

- Dossier par défaut : `/home/ludo/solarstation` (config et données persistantes).
- Heltec MeshCore : `/dev/ttyACM0`, 115200 bauds.
- Canal : `MeshCoreStation`, recherche via `commands.get_channel(0..7)` et champ `payload["channel_name"]`.
- INA219 : adresse `0x40` (64), résolution ADC 12 bits / 32 échantillons et plage bus `RANGE_16V`.
- La tension est exactement `bus_voltage` du pilote Adafruit, comme dans l'ancien `sensor.py`; aucun courant n'est lu et aucune valeur de shunt n'est ajoutée. L'offset réglable vaut zéro par défaut.
- Les clés historiques `ina219_address`, `intervalle_lecture` et `fichier_etat` sont conservées dans `config.json` et chargées telles quelles. Les valeurs absentes sont complétées par défauts dans `main.py`.

## Fonctionnalités

- Mesure rapide mult échantillon, filtrage médian robuste et lissage glissant pour stabiliser le relevé.
- Alerte à la première tension strictement inférieure à 12,9 V, rappel chaque 30 minutes tant que basse, message de retour à la normale dès 13,2 V inclus.
- Publication périodique de la tension toutes les deux heures (valeur `intervalle_envoi_tension`, en secondes).
- Commandes MeshCore `tension`, `etat`, `info`, `aide`, `ping`, `ping <secondes>` et `ping stop`. Les pings répétitifs ont un plancher de 30 s par défaut et une durée maximale de 30 min.
- Fichiers d'état/historique JSON, rotation simple, outbox locale et mode dégradé avec tentatives de reconnexion.
- `main.py --send message|meteo|info|alerte|donnee` alimente l'outbox (le démon reste seul propriétaire du port série).
- `mesh_send.py` permet un envoi ponctuel depuis un PC connecté au Heltec, commandes `message`, `meteo`, `info`, `alerte`, `donnee`.

## Installation sur le Pi

Copier tous les fichiers livrés dans `/home/ludo/solarstation`. Activer I2C et s'assurer que l'utilisateur `ludo` a accès au bus I2C et appartient au groupe `dialout` pour le port série.

```sh
cd /home/ludo/solarstation
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python main.py --config /home/ludo/solarstation/config.json
```

Le driver Adafruit CircuitPython configure les deux résolutions ADC à `ADCRES_12BIT_32S` et la plage bus à `RANGE_16V`. La logique ne collecte que `bus_voltage`, sans courant.

## Envoi local via outbox

Ces commandes déposent une demande dans `data/outbox`; elles ne prennent pas le port série :

```sh
python main.py --send message "Essai depuis le Pi"
python main.py --send info
python main.py --send alerte "Vérifier l'installation"
python main.py --send donnee
python main.py --send meteo
python main.py --send meteo Paris
python main.py --send meteo 48.86 2.35
```

## Envoi ponctuel depuis le PC

À utiliser uniquement si le démon n'utilise pas déjà le même Heltec :

```sh
python mesh_send.py --port /dev/ttyACM0 message "Bonjour"
python mesh_send.py meteo Paris
python mesh_send.py meteo --lat 48.86 --lon 2.35
python mesh_send.py info
python mesh_send.py alerte "Batterie à contrôler"
python mesh_send.py donnee
```

La météo requiert un accès Internet à Open-Meteo. `info` et `donnee` sur l'outil PC n'ont pas accès à l'état runtime du démon; l'outil l'indique explicitement.

## Compatibilité MeshCore

Les appels repris du `main.py` utilisateur sont `MeshCore.create_serial(port, baud, debug=True)`, `mesh.commands.get_channel(index)`, `mesh.commands.send_chan_msg(channel, text)`, `mesh.subscribe(EventType.CHANNEL_MSG_RECV, callback, attribute_filters={"channel_idx": canal})` et `await mesh.start_auto_message_fetching()`. Les signatures sont exercées par un faux MeshCore dans les tests; aucune radio n'est disponible en sandbox. La méthode `stop_auto_message_fetching` n'est pas dans l'extrait utilisateur : le programme ne l'appelle que si présente, puis tente `disconnect()`.

`solarstation.py` (fichier ancien dans le dépôt) fait double emploi avec `main.py`; ne le supprimez pas si vous le conservez déjà, mais pour cette version c'est `main.py` qui est le point d'entrée.

## Validation locale sans matériel

```sh
python -m py_compile main.py sensor.py battery.py meshbridge.py messages.py mesh_send.py test_solarstation.py
pytest -q test_solarstation.py
```
