"""Servidor ARCL del OMRON digital: el socket TCP que imita al del robot.

Un hilo por cliente (como `socketserver.ThreadingTCPServer`) y un hilo
reloj que avanza la simulación y reparte los eventos. Todo lo que toca el
robot va bajo un único candado: el modelo no es seguro entre hilos y así
no hace falta que lo sea.

Comportamiento visto en el LD real (2026-11-20):

- Al conectar: `Enter password:`.
- Contraseña mala: el robot corta la conexión con un RST, sin decir nada.
- Contraseña buena: `Welcome to the server.` y la lista de comandos.
- Fin de línea `\\r\\n` en todo lo que envía; acepta `\\n` o `\\r\\n`.
- `quit`: `Closing connection` y cierra.
"""
from __future__ import annotations

import logging
import socket
import socketserver
import struct
import threading
import time

from .arcl import BIENVENIDA, Sesion
from .robot import OmronDigital

log = logging.getLogger("omron_digital")


class _Cliente:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.candado = threading.Lock()      # el hilo reloj y el del cliente escriben a la vez

    def enviar(self, lineas: list[str]) -> bool:
        if not lineas:
            return True
        datos = "".join(l + "\r\n" for l in lineas).encode("utf-8")
        try:
            with self.candado:
                self.sock.sendall(datos)
            return True
        except OSError:
            return False


class ServidorARCL(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, direccion: tuple[str, int], robot: OmronDigital,
                 periodo: float = 0.1, acelerar: float = 1.0):
        super().__init__(direccion, _Manejador)
        self.robot = robot
        self.candado = threading.RLock()
        self.clientes: set[_Cliente] = set()
        self.periodo = periodo
        self.acelerar = acelerar
        self._parar = threading.Event()
        self._reloj = threading.Thread(target=self._bucle, name="omron-reloj", daemon=True)

    @property
    def puerto(self) -> int:
        return self.server_address[1]

    def arrancar(self) -> None:
        """Arranca el reloj y atiende conexiones en un hilo aparte (para tests y scripts)."""
        self._reloj.start()
        threading.Thread(target=self.serve_forever, name="omron-arcl", daemon=True).start()

    def servir(self) -> None:
        """Arranca el reloj y atiende conexiones en este hilo hasta Ctrl-C."""
        self._reloj.start()
        self.serve_forever()

    def parar(self) -> None:
        self._parar.set()
        self.shutdown()
        self.server_close()

    def _bucle(self) -> None:
        anterior = time.monotonic()
        while not self._parar.wait(self.periodo):
            ahora = time.monotonic()
            with self.candado:
                self.robot.avanzar((ahora - anterior) * self.acelerar)
                eventos, self.robot.eventos = self.robot.eventos, []
                clientes = list(self.clientes)
            anterior = ahora
            for e in eventos:
                log.info("evento: %s", e)
            for c in clientes:
                if not c.enviar(eventos):
                    self.clientes.discard(c)


class _Manejador(socketserver.StreamRequestHandler):
    server: ServidorARCL

    def handle(self) -> None:
        sock = self.request
        cliente = _Cliente(sock)
        cliente.enviar(["Enter password:"])
        linea = self.rfile.readline()
        if not linea or linea.decode("utf-8", "replace").strip() != self.server.robot.password:
            # SO_LINGER a 0: close() manda un RST, como el LD real
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            log.info("%s: contraseña incorrecta, conexión cortada", self.client_address[0])
            return
        log.info("%s: sesión ARCL abierta", self.client_address[0])
        cliente.enviar(BIENVENIDA)
        sesion = Sesion(self.server.robot)
        with self.server.candado:
            self.server.clientes.add(cliente)
        try:
            while not sesion.cerrar:
                linea = self.rfile.readline()
                if not linea:
                    break
                texto = linea.decode("utf-8", "replace")
                with self.server.candado:
                    respuesta = sesion.responder(texto)
                if texto.strip():
                    log.debug("%s> %s", self.client_address[0], texto.strip())
                if not cliente.enviar(respuesta):
                    break
        finally:
            with self.server.candado:
                self.server.clientes.discard(cliente)
            log.info("%s: sesión ARCL cerrada", self.client_address[0])
