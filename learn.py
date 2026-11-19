"""
learn.py — Aprender de las correcciones del usuario.

El problema que resuelve
────────────────────────
Whisper se equivoca con la terminología de la empresa. Hoy eso se arregla
editando `dictionary.json` a mano, que nadie hace. La idea es que corregir una
transcripción UNA vez baste para que no vuelva a fallar.

Qué NO hace, y por qué
──────────────────────
No guarda lo que dictaste. La transcripción vive en memoria hasta la
siguiente y nunca toca el disco; lo único que se persiste es el par de
términos aprendido. Así "aprender" no obliga a construir un historial.

El problema real: no toda corrección se debe aprender
─────────────────────────────────────────────────────
Si se aprendiera cada cambio, el diccionario se llenaría de basura: arreglos
de estilo, frases reescritas, palabras cambiadas por gusto. Y una entrada mala
corrompe texto correcto para siempre, que es el peor modo de falla posible.

Un cambio se aprende solo si parece una CORRECCIÓN DE TÉRMINO:

  boxfront      -> Workfront     SÍ, lo oído no existe en español
  instaje       -> intake        SÍ
  work front    -> Workfront     SÍ, palabras partidas que se unen
  rápido        -> veloz         NO, las dos son español legítimo (estilo)
  el informe    -> este informe  NO, reescritura
  reunion       -> reunión       NO, solo una tilde

El guard léxico es el mismo que usa terms.py: si lo que se oyó ya es una
palabra española válida, el cambio es estilo y no se aprende.
"""

import difflib
import io
import json
import os
import re

import terms

# Un término no se reescribe entero: si lo oído y lo corregido no se parecen
# en nada, es una frase distinta, no una corrección.
MAX_DIST_RATIO = 0.60
# Más de esto son palabras y ya no es un término, es una frase reescrita.
MAX_PALABRAS = 3
# Términos de una o dos letras generan ruido y falsos positivos.
MIN_LEN = 3

_PALABRA = re.compile(r"\w+", re.UNICODE)


def _tokens(texto):
    return _PALABRA.findall(texto)


def _es_termino_plausible(oido, corregido, lexico):
    """Decide si el par merece guardarse. Devuelve (bool, motivo)."""
    if not oido or not corregido:
        return False, "vacío"
    if len(oido.replace(" ", "")) < MIN_LEN or len(corregido.replace(" ", "")) < MIN_LEN:
        return False, "demasiado corto"
    if len(_tokens(oido)) > MAX_PALABRAS or len(_tokens(corregido)) > MAX_PALABRAS:
        return False, "es una frase, no un término"

    ko, kc = terms.clave(oido), terms.clave(corregido)
    if ko == kc:
        # clave() quita tildes Y espacios, así que esto colapsa dos casos muy
        # distintos. Hay que separarlos.
        if len(_tokens(oido)) != len(_tokens(corregido)):
            # "work front" -> "Workfront": palabra partida que hay que unir.
            # Es el error más común de Whisper con términos en inglés y SÍ se
            # aprende, sobre todo para que el término entre en `hints`: sin eso
            # el modelo lo sigue partiendo y hay que corregir cada variante.
            return True, "palabra partida que se une"
        # "reunion" -> "reunión": solo una tilde, no es un término nuevo.
        return False, "solo cambia acentuación o puntuación"

    # Guard léxico: si lo que se oyó YA es español válido, el usuario cambió
    # una palabra legítima por otra, y eso es estilo. Aprenderlo haría que la
    # app reemplace esa palabra en todo lo que dicte después.
    if lexico is not None and lexico.disponible:
        if all(lexico.es_palabra_real(p) for p in _tokens(oido)):
            return False, "lo oído ya es español válido (cambio de estilo)"

    largo = max(len(ko), len(kc))
    ratio = terms.levenshtein(ko, kc) / float(largo) if largo else 1.0
    if ratio > MAX_DIST_RATIO:
        return False, "demasiado distintos (ratio %.2f)" % ratio

    return True, "ratio %.2f" % ratio


def extraer_correcciones(original, corregido, lexico=None):
    """Compara las dos versiones y devuelve los pares que vale aprender.

    Devuelve (aprendibles, descartados), ambos listas de dicts con `oido`,
    `corregido` y `motivo`, para que la UI pueda mostrar también lo que
    decidió NO aprender y por qué.
    """
    a, b = _tokens(original), _tokens(corregido)
    sm = difflib.SequenceMatcher(None,
                                 [terms.clave(x) for x in a],
                                 [terms.clave(x) for x in b])
    aprendibles, descartados = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace":
            continue
        oido = " ".join(a[i1:i2])
        corr = " ".join(b[j1:j2])
        ok, motivo = _es_termino_plausible(oido, corr, lexico)
        registro = {"oido": oido, "corregido": corr, "motivo": motivo}
        (aprendibles if ok else descartados).append(registro)
    return aprendibles, descartados


def aplicar(correcciones, ruta_diccionario):
    """Escribe los pares aprendidos en dictionary.json.

    Cada par entra en las DOS capas, que es lo que lo hace funcionar:
      - `corrections` arregla ese error exacto desde la próxima vez
      - `hints` sesga el decoder hacia el término, así deja de oírlo mal

    Sin lo segundo solo se parchea el síntoma: el modelo seguiría produciendo
    variantes nuevas del mismo error y habría que corregir cada una.
    """
    with io.open(ruta_diccionario, encoding="utf-8") as f:
        d = json.load(f)
    corr = d.setdefault("corrections", {})
    hints = d.setdefault("hints", [])

    nuevas, ya_estaban = [], []
    for c in correcciones:
        clave_oida = c["oido"].lower()
        if corr.get(clave_oida) == c["corregido"]:
            ya_estaban.append(c)
            continue
        corr[clave_oida] = c["corregido"]
        nuevas.append(c)
        if c["corregido"] not in hints:
            hints.append(c["corregido"])

    if nuevas:
        tmp = ruta_diccionario + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, ruta_diccionario)   # atómico: nunca deja el archivo a medias
    return nuevas, ya_estaban
