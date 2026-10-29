# Decisor de órdenes (`app/interprete.py`, experimental)

```mermaid
flowchart TD
    W["Transcripción de Whisper<br/><i>trae de la estarentia 2 diferencuiales</i>"] --> R

    subgraph CFG["Lista de misiones (se recarga sola)"]
        MJ[("missions.json<br/>o GET /api/config")] -->|¿cambió?| R["refresh()<br/>reconstruye formas fonéticas,<br/>prompt del LLM y esquema JSON"]
    end

    R --> PH & LLM

    subgraph PAR["Dos lecturas independientes"]
        PH["<b>Comparador fonético</b> ~0 ms<br/>3 primeras palabras, también unidas: <i>de ja → deja</i><br/>parecido con las palabras de cada misión<br/>el ganador necesita ≥ 0,10 de ventaja, si no none"]
        LLM["<b>LLM</b> Qwen3-1.7B, ~650 ms<br/>JSON: corrected, mission,<br/>object, quantity, zone, confidence<br/>(limitado al vocabulario)"]
    end

    PH --> C1
    LLM --> C1

    C1{"¿Los dos nombran una misión<br/>y no coinciden?"} -->|sí| REP
    C1 -->|no| C2{"¿El LLM nombra<br/>una misión?"}
    C2 -->|sí| M["misión = la del LLM"]
    C2 -->|no| C3{"¿El fonético nombra una<br/>con parecido ≥ 0,70?"}
    C3 -->|sí| M2["misión = la del fonético"]
    C3 -->|no| REP

    M --> V1
    M2 --> V1
    V1{"Veto de verbo:<br/>¿otra misión suena<br/>≥ 0,15 más parecida?"} -->|sí| REP
    V1 -->|no| V2{"¿Orden inmediata (parar)<br/>pero se oye un objeto?<br/><i>pra esa caja</i>"}
    V2 -->|sí| REP
    V2 -->|no, inmediata| EX
    V2 -->|no, misión con objeto| F

    F["<b>Filtro de campos</b><br/>objeto / zona solo si se oyen en la frase<br/>(parecido ≥ 0,72, palabras de ≥ 4 letras)<br/><i>estarentia ✓ estantería · destornillador ✗ diferencial</i><br/>sin objeto → sin cantidad"] --> C4{"¿Hay<br/>objeto?"}
    C4 -->|sí| EX
    C4 -->|no| REP

    EX(["<b>EJECUTAR</b><br/>misión + objeto + cantidad + zona"])
    REP(["<b>PEDIR QUE REPITA</b>"])

    EX --> D["Frase del dashboard construida con los campos<br/><i>trae dos diferenciales de la estantería</i><br/>junto al texto original de Whisper"]
    REP --> D

    classDef ok fill:#d4edda,stroke:#2e7d32,color:#1b5e20
    classDef stop fill:#fff3cd,stroke:#b8860b,color:#5d4300
    class EX ok
    class REP stop
```

| Umbral | Valor | Qué controla |
|---|---|---|
| `PHONETIC_MARGIN` | 0,10 | el ganador fonético debe superar al segundo por esto |
| `STRONG_MATCH` | 0,70 | el fonético decide solo (el LLM dijo none) solo por encima de esto |
| `VETO_MARGIN` | 0,15 | se veta si otra misión suena esto más parecida |
| `FIELD_MATCH` | 0,72 | objeto/zona deben sonar así de parecidos a una palabra de la frase |
