## Done

- Server health on page (whisper badge, polls `/health` every 5 s)
- Hands-free wake word "tonto" + voice confirmation "adelante"
- Confirmation/ARMADO windows 10 s / 8 s, paused while speaking and while whisper transcribes
- Voice log `logs/voice.jsonl` + `logs/audio/` (phrases, orders, cancels) to build real test cases

## Pending

1. **Whisper prompt with the commands** — done: word list in `.env` (148 → 164/197 right in
   `../llms/bench_audio.py`, same wrong actions). Pending: build it from `missions.json` automatically;
   "adelante" left out on purpose (whisper can copy prompt words into noisy audio → false confirm).
2. **Order matching** — "traes" does not match "trae". Either add variants to `missions.json`
   or match command words with edit distance ≤ 1 (watch out for false positives on short words).
3. **More commands?** — e.g. "muévete" is "no entendido"; decide which mission it maps to.
4. **Test "tonto, para"** (cancelOrder on the robot of the last order) on the simulated fleet.
5. **Whisper latency** — measured 3.6 s for a 1 s WAV vs ~200 ms documented; check with the whisper owner.
6. **Beep** — silent on the user's Windows machine while its audio output is blocked.
7. **Debug logs** — turn off `DEBUG` in `app/static/index.html` once stable.
8. **Cleanup** — unused Docker image `mission-manager-test`; repo has no commits yet; add `.pytest_cache/` to `.gitignore`.
