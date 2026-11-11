"""Lecture robuste d'un INA219, en tension uniquement.

Le réglage matériel et le calcul de tension reprennent le capteur.py du projet
SolarStation existant : plage bus 16 V, ADC 12 bits / 32 échantillons, et
lecture directe de ``bus_voltage``. Aucun courant, puissance ou shunt n'est
exposé ou ajouté à la tension. Lissage médian destiné à réduire le bruit.
"""
import logging
import math
import statistics
import time

logger = logging.getLogger(__name__)


class INA219Sensor:
    """Capteur INA219 avec échantillonnage, reprises I2C et lissage glissant.

    ``driver`` permet d'injecter un faux capteur dans les tests, sans importer
    les bibliothèques matérielles. En utilisation normale celles-ci ne sont
    chargées qu'à l'instanciation, ce qui permet au reste du programme de
    démarrer et de fonctionner en mode dégradé en leur absence.
    """
    def __init__(self, i2c_address=0x40, offset_volts=0.0, samples=5, window=5,
                 retries=2, retry_delay=0.05, sample_delay=0.01, driver=None,
                 i2c=None):
        self.i2c_address = int(i2c_address, 0) if isinstance(i2c_address, str) else int(i2c_address)
        self.offset_volts = float(offset_volts)
        self.samples = max(1, int(samples))
        self.window = max(1, int(window))
        self.retries = max(0, int(retries))
        self.retry_delay = max(0.0, float(retry_delay))
        self.sample_delay = max(0.0, float(sample_delay))
        self._history = []
        self.i2c = i2c
        self.ina219 = driver

        if self.ina219 is None:
            import board
            import busio
            from adafruit_ina219 import ADCResolution, BusVoltageRange, INA219

            self.i2c = busio.I2C(board.SCL, board.SDA)
            self.ina219 = INA219(self.i2c, addr=self.i2c_address)
            # Ces paramètres reprennent ceux du sensor.py du dépôt utilisateur.
            self.ina219.bus_adc_resolution = ADCResolution.ADCRES_12BIT_32S
            self.ina219.shunt_adc_resolution = ADCResolution.ADCRES_12BIT_32S
            self.ina219.bus_voltage_range = BusVoltageRange.RANGE_16V

        logger.info("INA219 initialisé à l'adresse 0x%02x (plage 16 V)", self.i2c_address)

    @staticmethod
    def _median_mean(values):
        """Retire les valeurs aberrantes par MAD, puis moyenne les restantes."""
        median = statistics.median(values)
        mad = statistics.median(abs(value - median) for value in values)
        if mad:
            limite = 3.5 * 1.4826 * mad
            retenues = [value for value in values if abs(value - median) <= limite]
        else:
            retenues = [value for value in values if abs(value - median) < 1e-9]
        return statistics.fmean(retenues or [median])

    def get_voltage(self):
        """Renvoie la tension bus (et non une tension recalculée avec le shunt)."""
        readings = []
        last_error = None
        for index in range(self.samples):
            value = None
            for attempt in range(self.retries + 1):
                try:
                    # Fidèle à l'ancien sensor.py : ``bus_voltage`` seulement.
                    value = float(self.ina219.bus_voltage) + self.offset_volts
                    if not math.isfinite(value) or not 0.0 <= value <= 16.0:
                        raise ValueError("mesure INA219 hors plage 0–16 V")
                    break
                except Exception as exc:
                    last_error = exc
                    value = None
                    if attempt < self.retries:
                        time.sleep(self.retry_delay)
            if value is not None:
                readings.append(value)
            if index + 1 < self.samples and self.sample_delay:
                time.sleep(self.sample_delay)

        if not readings:
            raise IOError(f"Lecture INA219 impossible après reprises: {last_error}")
        cycle_value = self._median_mean(readings)
        self._history.append(cycle_value)
        self._history = self._history[-self.window:]
        return self._median_mean(self._history)

    def close(self):
        """Libère le bus I2C, si le pilote fournit une méthode de fermeture."""
        try:
            if self.i2c is not None and hasattr(self.i2c, "deinit"):
                self.i2c.deinit()
        except Exception:
            logger.debug("Fermeture I2C non nécessaire", exc_info=True)
