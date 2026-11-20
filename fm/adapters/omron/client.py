"""Cliente ARCL de un OMRON (LD/HD/MD): socket TCP de texto, puerto 7171.

ARCL no es petición/respuesta limpia: por el mismo socket llegan, mezclados
con las respuestas, los mensajes que el robot reparte a todos los clientes
("Going to X", "QueueUpdate: …", "Completed macro …"). Por eso cada comando
dice qué líneas son suyas (`acepta`) y cuál es la última (`fin`); el resto
se guarda en `eventos` para que el driver las procese en su `poll()`.

Comportamiento visto en el LD real (2026-11-20, ver simuladores/omron/):
- Pide `Enter password:`; con una clave mala corta la conexión (RST).
- Tras la clave, la bienvenida y la lista de comandos hasta `End of commands`.
- Un comando que falla responde `CommandError: <línea>` y
  `CommandErrorDescription: <motivo>`.
- Por wifi cada respuesta tarda 0,2–1 s: se espera a la línea final, nunca
  un tiempo fijo.

Nombres de método: el comando ARCL tal cual (`status`, `get_goals`…), para
poder buscar en el código por el comando.
"""
from __future__ import annotations

import socket
import threading
import time
from collections import deque
from typing import Callable

Predicado = Callable[[str], bool]


class ArclError(Exception):
    """El robot respondió `CommandError` (lleva la descripción)."""


def _empieza(*prefijos: str) -> Predicado:
    return lambda l: l.startswith(prefijos)


class ArclClient:
    def __init__(self, host: str, password: str, port: int = 7171, timeout: float = 5.0):
        self.host, self.port, self.password, self.timeout = host, port, password, timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._lock = threading.RLock()          # el hilo de poll y el principal mandan comandos
        self.eventos: deque[str] = deque(maxlen=2000)

    # --- conexión ---

    @property
    def conectado(self) -> bool:
        return self._sock is not None

    def conectar(self) -> None:
        with self._lock:
            self.cerrar()
            s = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._sock, self._buf = s, b""
            try:
                self._esperar(lambda l: l.startswith("Enter password"), self.timeout)
                self._enviar(self.password)
                self._esperar(lambda l: l == "End of commands", self.timeout * 2)
            except Exception:
                self.cerrar()
                raise

    def cerrar(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
            self._sock, self._buf = None, b""

    # --- bajo nivel ---

    def _enviar(self, linea: str) -> None:
        assert self._sock is not None
        self._sock.sendall((linea + "\r\n").encode("utf-8"))

    def _linea(self, limite: float) -> str | None:
        """Siguiente línea completa, o None si se acaba el tiempo."""
        while b"\n" not in self._buf:
            resto = limite - time.monotonic()
            if resto <= 0:
                return None
            self._sock.settimeout(resto)
            try:
                trozo = self._sock.recv(65536)
            except socket.timeout:
                return None
            if not trozo:
                raise ConnectionError("el robot cerró la conexión")
            self._buf += trozo
        linea, self._buf = self._buf.split(b"\n", 1)
        return linea.decode("utf-8", "replace").rstrip("\r")

    def _esperar(self, fin: Predicado, timeout: float) -> list[str]:
        limite = time.monotonic() + timeout
        lineas = []
        while True:
            l = self._linea(limite)
            if l is None:
                raise TimeoutError("sin respuesta del robot")
            lineas.append(l)
            if fin(l):
                return lineas

    # --- comandos ---

    def comando(self, linea: str, acepta: Predicado | None, fin: Predicado | None,
                timeout: float | None = None) -> list[str]:
        """Manda `linea` y devuelve las líneas de su respuesta (las que cumplen
        `acepta`) hasta la que cumple `fin`. Sin `fin`, no espera respuesta.
        Lanza ArclError si el robot contesta CommandError, y OSError/TimeoutError
        (cerrando el socket) si falla la red."""
        with self._lock:
            if self._sock is None:
                raise ConnectionError("sin conexión ARCL")
            try:
                self._enviar(linea)
                if fin is None:
                    return []
                limite = time.monotonic() + (timeout or self.timeout)
                propias: list[str] = []
                while True:
                    l = self._linea(limite)
                    if l is None:
                        raise TimeoutError(f"sin respuesta a '{linea}'")
                    if l.startswith("CommandError:"):
                        desc = self._linea(limite) or ""
                        raise ArclError(desc.partition(": ")[2] or desc)
                    if acepta is not None and acepta(l):
                        propias.append(l)
                        if fin(l):
                            return propias
                    elif l.strip():
                        self.eventos.append(l)
            except ArclError:
                raise
            except (OSError, ConnectionError, TimeoutError):
                self.cerrar()
                raise

    def leer_eventos(self) -> list[str]:
        """Recoge lo que haya llegado sin pedirlo y vacía `eventos`."""
        with self._lock:
            if self._sock is not None:
                try:
                    while (l := self._linea(time.monotonic() + 0.05)) is not None:
                        if l.strip():
                            self.eventos.append(l)
                except OSError:
                    self.cerrar()
            salida = list(self.eventos)
            self.eventos.clear()
            return salida

    # --- comandos concretos (nombre = comando ARCL) ---

    def status(self) -> dict[str, str]:
        lineas = self.comando("status", _empieza("ExtendedStatusForHumans:", "Status:", "StateOfCharge:",
                                                 "Location:", "LocalizationScore:", "Temperature:"),
                              _empieza("Temperature:"))
        return {k: v.strip() for k, _, v in (l.partition(":") for l in lineas)}

    def get_goals(self) -> list[str]:
        lineas = self.comando("getGoals", _empieza("Goal:", "End of goals"), lambda l: l == "End of goals")
        return [l.partition(": ")[2] for l in lineas if l.startswith("Goal:")]

    def get_macros(self) -> list[str]:
        lineas = self.comando("getMacros", _empieza("Macro:", "End of macros"), lambda l: l == "End of macros")
        return [l.partition(": ")[2] for l in lineas if l.startswith("Macro:")]

    def get_config_section_values(self, seccion: str) -> dict[str, str]:
        lineas = self.comando(f"getConfigSectionValues {seccion}",
                              _empieza("GetConfigSectionValue:", "EndOfGetConfigSectionValues"),
                              _empieza("EndOfGetConfigSectionValues"))
        valores = {}
        for l in lineas:
            partes = l.split(None, 2)
            if len(partes) >= 2 and partes[0] == "GetConfigSectionValue:":
                valores[partes[1]] = partes[2] if len(partes) == 3 else ""
        return valores

    def queue_pickup(self, goal: str, prioridad: int, job_id: str) -> str:
        lineas = self.comando(f'queuePickup "{goal}" {prioridad} {job_id}', _empieza("queuepickup"),
                              _empieza("queuepickup"))
        return lineas[-1]

    def queue_query(self, job_id: str) -> list[str]:
        """Sin `Local`: con Local, un trabajo devuelto a la cola sin robot
        (tras una pausa) deja de aparecer (visto en el LD)."""
        return self.comando(f"queueQuery jobid {job_id}", _empieza("QueueQuery:", "EndQueueQuery"),
                            lambda l: l == "EndQueueQuery")

    def queue_cancel(self, job_id: str) -> None:
        self.comando(f"queueCancel jobid {job_id}", _empieza("queuecancel"),
                     lambda l: "from queue" in l or "failed" in l)

    def execute_macro_template(self, nombre: str, parametros: list[str]) -> None:
        linea = " ".join(["executeMacroTemplate", nombre, *parametros])
        self.comando(linea, _empieza(f"Executing macro template {nombre}"),
                     _empieza(f"Executing macro template {nombre}"))

    def execute_macro(self, nombre: str) -> None:
        self.comando(f"executeMacro {nombre}", _empieza(f"Executing macro {nombre}"),
                     _empieza(f"Executing macro {nombre}"))

    def stop(self) -> None:
        self.comando("stop", None, None)

    def application_block_driving_set(self, nombre: str, corto: str, largo: str) -> None:
        self.comando(f'applicationBlockDrivingSet {nombre} "{corto}" "{largo}"',
                     _empieza("ApplicationBlockDrivingSet"), _empieza("ApplicationBlockDrivingSet"))

    def application_block_driving_clear(self, nombre: str) -> None:
        self.comando(f"applicationBlockDrivingClear {nombre}",
                     _empieza("ApplicationBlockDrivingClear"), _empieza("ApplicationBlockDrivingClear"))
