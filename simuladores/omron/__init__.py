"""OMRON LD digital: un servidor ARCL que responde como el robot real.

Sirve para desarrollar y probar el driver OMRON del FM (docs/plan-omron.md)
sin tener el robot delante. Ver README.md.
"""
from .mapa import Mapa, leer_mapa
from .robot import OmronDigital
from .servidor import ServidorARCL

__all__ = ["Mapa", "leer_mapa", "OmronDigital", "ServidorARCL"]
