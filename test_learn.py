"""
test_learn.py — Verifica qué correcciones se aprenden y cuáles NO.

    python test_learn.py

El riesgo de esta función es asimétrico. No aprender una corrección solo
significa que el usuario la repite; aprender una que no debía mete una regla
permanente que corrompe texto correcto en cada dictado futuro. Por eso los
FALSOS POSITIVOS invalidan la corrida y los negativos solo se reportan.
"""
import os
import sys

import learn
import terms

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Errores reales de Whisper medidos sobre voz en español con ruido.
DEBEN_APRENDERSE = [
    ("Revisa el boxfront de la cuenta", "Revisa el Workfront de la cuenta", "boxfront"),
    ("Hicimos el instaje del requerimiento", "Hicimos el intake del requerimiento", "instaje"),
    ("Llama a la API de Antropi", "Llama a la API de Anthropic", "Antropi"),
    ("falta el deploiment en produccion", "falta el deployment en produccion", "deploiment"),
    ("Revisa el work front de la cuenta", "Revisa el Workfront de la cuenta", "work front"),
    ("usa el chat bot nuevo", "usa el chatbot nuevo", "chat bot"),
]

# Ediciones legítimas que NO son correcciones de término. Aprender cualquiera
# de estas dejaría la app reemplazando palabras correctas para siempre.
NO_DEBEN_APRENDERSE = [
    ("El proceso es muy rapido", "El proceso es muy veloz", "cambio de estilo"),
    ("la reunion es manana", "la reunión es mañana", "solo tildes"),
    ("el informe quedo listo", "este informe quedo listo", "reescritura corta"),
    ("Vamos a revisar el presupuesto", "Hay que mirar otra cosa distinta", "frase entera"),
    ("mandé el correo ayer", "mandé el mensaje ayer", "sinónimo legítimo"),
    ("la clave de acceso caduca", "la contraseña de acceso caduca", "sinónimo legítimo"),
    ("revisamos los temas pendientes", "revisamos los asuntos pendientes", "sinónimo legítimo"),
]


def main():
    lex = terms.Lexico()
    print("backend léxico: %s" % lex.backend)
    if not lex.disponible:
        print("   AVISO: sin léxico no se puede filtrar el cambio de estilo,")
        print("   así que esta corrida no prueba lo que cree probar.")
    print()

    fallos = 0

    print("=== DEBEN APRENDERSE ===")
    ok = 0
    for original, corregido, esperado in DEBEN_APRENDERSE:
        apr, _desc = learn.extraer_correcciones(original, corregido, lex)
        encontrado = any(c["oido"].lower() == esperado.lower() for c in apr)
        ok += encontrado
        print("  %s %-14s -> %s" % (
            "OK " if encontrado else ">>>", esperado,
            ", ".join("%s=%s" % (c["oido"], c["corregido"]) for c in apr) or "(nada)"))
    print("  %d de %d" % (ok, len(DEBEN_APRENDERSE)))
    if ok < len(DEBEN_APRENDERSE):
        fallos += 1

    print("\n=== NO DEBEN APRENDERSE (un solo fallo invalida la corrida) ===")
    colados = []
    for original, corregido, por_que in NO_DEBEN_APRENDERSE:
        apr, _desc = learn.extraer_correcciones(original, corregido, lex)
        if apr:
            colados.append((por_que, apr))
    print("  %d de %d rechazadas" % (
        len(NO_DEBEN_APRENDERSE) - len(colados), len(NO_DEBEN_APRENDERSE)))
    for por_que, apr in colados:
        print("     SE COLÓ (%s): %s" % (
            por_que, ", ".join("%s -> %s" % (c["oido"], c["corregido"]) for c in apr)))
    if colados:
        print("     FALLA CRÍTICA: se aprendería una edición que no es de término")
        fallos += 1

    print("\n%s" % ("TODO OK" if fallos == 0 else "%d GRUPO(S) CON FALLA" % fallos))
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
