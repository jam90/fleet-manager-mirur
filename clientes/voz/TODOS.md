## Hecho

- Salud del servidor en la página (indicador de whisper, consulta `/health` cada 5 s)
- Manos libres con palabra de activación "tonto" + confirmación por voz "adelante"
- Ventanas de confirmación/ARMADO de 10 s / 8 s, en pausa mientras se habla y mientras whisper transcribe
- Registro de voz `logs/voice.jsonl` + `logs/audio/` (frases, orders, cancelaciones) para construir casos de prueba reales

## Pendiente

1. **Prompt de Whisper con los comandos**: hecho, con una lista de palabras en `.env` (148 → 164/197 aciertos en
   `../llms/bench_audio.py`, mismas acciones erróneas). Falta generarlo automáticamente desde `missions.json`.
   "adelante" se deja fuera a propósito: whisper puede copiar palabras del prompt en audio con ruido y eso
   daría confirmaciones falsas.
2. **Coincidencia de órdenes**: "traes" no coincide con "trae". O se añaden variantes a `missions.json`,
   o se comparan las palabras de la orden con distancia de edición ≤ 1 (ojo con los falsos positivos en palabras cortas).
3. **¿Más comandos?**: p. ej. "muévete" da "no entendido"; decidir a qué misión corresponde.
4. **Probar "tonto, para"** con la voz real en la flota simulada. La ruta ya está probada sin
   voz (2026-10-30): cancela la última order en la cola del FM o en su robot.
5. **Latencia de Whisper**: se midieron 3,6 s para un WAV de 1 s frente a los ~200 ms documentados; revisarlo con quien lleva whisper.
6. **Pitido**: no suena en el Windows del usuario mientras su salida de audio está bloqueada.
7. **Logs de depuración**: quitar `DEBUG` en `app/static/index.html` cuando esté estable.
8. **Limpieza**: imagen Docker sin usar `mission-manager-test`; el repo aún no tiene commits; añadir `.pytest_cache/` a `.gitignore`.
