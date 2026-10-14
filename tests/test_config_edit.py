"""Edición de fleet.yaml: solo cambia el valor editado; el resto (comentarios,
formato) queda byte a byte; se valida antes de escribir y se deja copia."""
from datetime import datetime
from pathlib import Path

import pytest

from fm.config import ConfigError
from fm.config_edit import EditError, apply_edits, save

YAML = """\
# Comentario de cabecera: debe sobrevivir.
auto_charge:
  battery_floor: 20            # % — por debajo se encola la carga
  priority: 10

actions:
  coger: { mission: coger }    # el formato { a } no se normaliza
  dejar: { mission: dejar }
  ir_a:  { mission: ir }

robots:
  sim-1:
    driver: sim
    battery_min: 25
    actions: [coger, dejar,
              ir_a]   # lista partida en dos líneas
  sim-2:
    driver: sim
    actions: [coger]
"""


def _lines_changed(a: str, b: str) -> list[tuple[str, str]]:
    return [(x, y) for x, y in zip(a.splitlines(), b.splitlines()) if x != y]


def test_solo_cambia_el_valor_y_el_comentario_sigue_en_su_columna():
    new = apply_edits(YAML, {"auto_charge": {"battery_floor": 18.5}})
    assert _lines_changed(YAML, new) == [("  battery_floor: 20            # % — por debajo se encola la carga",
                                          "  battery_floor: 18.5          # % — por debajo se encola la carga")]


def test_lista_partida_se_reescribe_en_una_linea_con_su_comentario():
    new = apply_edits(YAML, {"robots": {"sim-1": {"actions": ["coger"]}}})
    assert "    actions: [coger]  # lista partida en dos líneas\n" in new     # el # sigue en la columna 22
    assert "  coger: { mission: coger }    # el formato { a } no se normaliza" in new
    assert new.count("\n") == YAML.count("\n") - 1


def test_clave_ausente_se_inserta_con_la_sangria_del_bloque():
    new = apply_edits(YAML, {"robots": {"sim-2": {"battery_min": 30}}, "assigner": {"prefer_not_charging": False}})
    assert "  sim-2:\n    driver: sim\n    actions: [coger]\n    battery_min: 30\n" in new   # última de su bloque
    assert new.endswith("\nassigner:\n  prefer_not_charging: false\n")


@pytest.mark.parametrize("edits, msg", [
    ({"robots": {"sim-1": {"host": "x"}}}, "no se puede editar"),
    ({"robots": {"nadie": {"battery_min": 30}}}, "no está en fleet.yaml"),
    ({"robots": {"sim-1": {"battery_min": "mucho"}}}, "número"),
    ({"robots": {"sim-1": {"actions": "coger"}}}, "lista"),
    ({"auto_charge": {"priority": 2.5}}, "entero"),
    ({"assigner": {"prefer_not_charging": "sí"}}, "true o false"),
    ({"mqtt": {"host": "x"}}, "no se puede editar"),
])
def test_ediciones_invalidas(edits, msg):
    with pytest.raises(EditError, match=msg):
        apply_edits(YAML, edits)


def test_save_valida_hace_copia_y_escribe(tmp_path):
    p = tmp_path / "fleet.yaml"
    p.write_text(YAML, encoding="utf-8")
    res = save(p, {"robots": {"sim-2": {"actions": ["coger", "ir_a"]}}}, env_path=tmp_path / "no.env",
               now=datetime(2026, 10, 14, 9, 30, 0))
    assert res.changed and res.backup.name == "fleet.yaml.20261014-093000.bak"
    assert res.backup.read_text(encoding="utf-8") == YAML
    assert res.cfg.robots["sim-2"].action_types == {"coger", "ir_a"}
    assert "    actions: [coger, ir_a]\n" in p.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp"))


def test_save_sin_cambios_no_escribe(tmp_path):
    p = tmp_path / "fleet.yaml"
    p.write_text(YAML, encoding="utf-8")
    res = save(p, {"robots": {"sim-1": {"battery_min": 25}}}, env_path=tmp_path / "no.env")
    assert not res.changed and res.backup is None and not list(tmp_path.glob("*.bak"))


def test_save_invalido_no_toca_el_fichero(tmp_path):
    p = tmp_path / "fleet.yaml"
    p.write_text(YAML, encoding="utf-8")
    with pytest.raises(ConfigError, match="no están en el catálogo"):
        save(p, {"robots": {"sim-1": {"actions": ["coger", "volar"]}}}, env_path=tmp_path / "no.env")
    assert p.read_text(encoding="utf-8") == YAML
    assert not list(tmp_path.glob("*.bak")) and not list(tmp_path.glob("*.tmp"))


def test_fleet_yaml_del_repo_se_edita_sin_tocar_nada_mas():
    text = (Path(__file__).resolve().parent.parent / "config" / "fleet.yaml").read_text(encoding="utf-8")
    new = apply_edits(text, {"robots": {"mir-1": {"battery_min": 31}}})
    assert len(_lines_changed(text, new)) == 1 and len(new) == len(text)



# ------------------------------------------------- catálogo de actions (v2)
CAT = """\
actions:
  coger:
    mission: GUID-1   # coger
  abrir:                 # comentario de la action: se conserva
    mission: GUID-6   # Abrir puerta
    position_inputs: [target_pos]
    positions_allowlist: [A, B]   # solo estas
    number_inputs:
      n: { min: 1, max: 10 }        # rango

# Comentario entre bloques: debe quedarse aquí.
robots:
  sim-1:
    driver: sim
    actions: [coger]
"""


def _act(definition, **comments):
    return {"definition": definition, "comments": comments}


def test_cambiar_una_clave_conserva_el_resto_de_la_action():
    new = apply_edits(CAT, {"actions": {"abrir": _act({
        "mission": "GUID-6", "position_inputs": ["target_pos"], "positions_allowlist": ["A", "C"],
        "number_inputs": {"n": {"min": 1, "max": 10}}})}})
    assert _lines_changed(CAT, new) == [("    positions_allowlist: [A, B]   # solo estas",
                                         "    positions_allowlist: [A, C]   # solo estas")]


def test_cambiar_la_mission_actualiza_su_comentario():
    new = apply_edits(CAT, {"actions": {"coger": _act({"mission": "GUID-2"}, mission="Otra mission")}})
    assert "    mission: GUID-2   # Otra mission\n" in new and len(_lines_changed(CAT, new)) == 1


def test_quitar_claves_y_cambiar_un_bloque():
    new = apply_edits(CAT, {"actions": {"abrir": _act({
        "mission": "GUID-6", "number_inputs": {"n": {"min": 2}, "m": {}}})}})
    assert "position_inputs" not in new and "positions_allowlist" not in new
    assert "  abrir:                 # comentario de la action: se conserva\n    mission: GUID-6   # Abrir puerta\n" \
           "    number_inputs:\n      n: {min: 2}\n      m: {}\n\n# Comentario entre bloques" in new


def test_action_nueva_al_final_del_catalogo_y_habilitada_a_la_vez():
    new = apply_edits(CAT, {
        "actions": {"traer": _act({"mission": "GUID-11", "number_inputs": {"n": {"min": 1, "max": 5, "step": 1}}},
                                  mission="traer")},
        "robots": {"sim-1": {"actions": ["coger", "traer"]}}})
    assert ("      n: { min: 1, max: 10 }        # rango\n"
            "  traer:\n    mission: GUID-11   # traer\n    number_inputs:\n      n: {min: 1, max: 5, step: 1}\n"
            "\n# Comentario entre bloques") in new
    assert "    actions: [coger, traer]\n" in new


def test_borrar_action_quita_sus_lineas():
    new = apply_edits(CAT, {"actions": {"abrir": None}})
    assert new == CAT.replace(CAT[CAT.index("  abrir:"):CAT.index("\n# Comentario entre")], "")
    assert apply_edits(CAT, {"actions": {"no-existe": None}}) == CAT


def test_definicion_vacia_y_en_flujo():
    text = "actions:\n  coger:\n  dejar: { mission: d }\nrobots: {}\n"
    new = apply_edits(text, {"actions": {"coger": _act({"mission": "c"}, mission="Coger"),
                                         "dejar": _act({"mission": "d2"})}})
    # la de flujo se edita clave a clave: conserva su estilo `{ ... }`
    assert new == "actions:\n  coger:\n    mission: c   # Coger\n  dejar: { mission: d2 }\nrobots: {}\n"


@pytest.mark.parametrize("edits, msg", [
    ({"actions": {"con espacio": _act({})}}, "no vale como nombre"),
    ({"actions": {"x": {"definition": "texto"}}}, "se espera"),
    ({"actions": {"x": _act({"mission": "m"}, mission="dos\nlíneas")}}, "una línea"),
])
def test_ediciones_del_catalogo_invalidas(edits, msg):
    with pytest.raises(EditError, match=msg):
        apply_edits(CAT, edits)


def test_borrar_una_action_habilitada_no_se_guarda(tmp_path):
    p = tmp_path / "fleet.yaml"
    p.write_text(CAT, encoding="utf-8")
    with pytest.raises(ConfigError, match="no están en el catálogo"):
        save(p, {"actions": {"coger": None}}, env_path=tmp_path / "no.env")
    assert p.read_text(encoding="utf-8") == CAT


def test_catalogo_del_repo_editar_una_action_toca_una_linea():
    text = (Path(__file__).resolve().parent.parent / "config" / "fleet.yaml").read_text(encoding="utf-8")
    import yaml
    d = yaml.safe_load(text)["actions"]["prueba_parametros_v2"]
    d = {**d, "positions_allowlist": ["Deshecho", "Montaje"]}
    new = apply_edits(text, {"actions": {"prueba_parametros_v2": _act(d)}})
    assert len(_lines_changed(text, new)) == 1
