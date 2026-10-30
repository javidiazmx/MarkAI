# Cómo darle a tu bot de Grok el contexto de GC Realty

`mark export` genera dos archivos. **Son dos a propósito, y la diferencia importa.**

| Archivo | Dónde va | Por qué |
|---|---|---|
| `gc-context-instructions.md` | Las **Custom Instructions** de Grok | Persisten en **todas** las conversaciones |
| `gc-context-pack.md` | Subido como **archivo adjunto** | Solo aplica donde lo adjuntas |

Las reglas de seguridad tienen que estar en la parte que siempre se carga. Si las metes
dentro del archivo, el primer chat que abras sin adjuntarlo es un bot respondiendo una
pregunta de Chicago con el plazo de notificación de Evanston — que es una respuesta
equivocada, no una respuesta faltante.

## Generar los archivos

En `cmd`, dentro de `C:\Users\javid\MarkAI`:

```cmd
python -m markai export
```

Los deja en tu carpeta `data\`. Si los quieres en otro lado:

```cmd
python -m markai export --out docs\grok
```

El comando te dice en pantalla qué entradas dejó fuera y por qué. No oculta nada.

## Cargarlos en Grok

1. **Las instrucciones.** Abre Grok → Settings → Customize / Custom Instructions. Pega
   el contenido completo de `gc-context-instructions.md`. Son unos 2,900 caracteres, así
   que entra aunque el límite esté en 4,000.
2. **El paquete.** Adjunta `gc-context-pack.md` a la conversación. Son ~260 KB, muy por
   debajo del límite de archivos de Grok.
3. **Si usas Workspaces / Projects** (recomendado): crea uno llamado *GC Realty*, pon las
   instrucciones ahí y sube el archivo al workspace. Así el archivo queda adjunto a cada
   conversación de ese workspace y no tienes que volver a subirlo.

Verifica que funcionó con una pregunta que tenga trampa:

> *Tengo un edificio en Evanston. ¿Cuánto aviso debo dar para no renovar?*

Una respuesta correcta dice **90 días**, cita `mined-35`, y termina con la frase del
disclaimer. Si contesta 60 días (que es Cook County) o no cita ningún id, las instrucciones
no se cargaron.

## Qué contiene el paquete

544 entradas revisadas por ti, agrupadas **por jurisdicción** y no por tema. Esa decisión
es deliberada: una pregunta de Evanston sobre avisos tiene tres respuestas correctas según
el motivo — 10 días por falta de pago, 30 por violación del contrato, 90 por no renovar — y
agrupar por jurisdicción las deja juntas para que el modelo vea la bifurcación. Agrupadas
por tema quedarían a cientos de líneas de distancia, y el modelo contestaría con la que
encontró primero. Eso ya pasó una vez en tu propio Notice Wizard; la nota de `mined-546` lo
documenta.

Cada entrada lleva su cita textual, su fuente y su id. El id es la parte importante: las
instrucciones obligan al bot a terminar cada frase factual con el id del que salió, así que
cualquier invento queda sin id y se detecta con un Ctrl-F. Es la única protección que
funciona aunque quien lea la respuesta no sepa de leyes.

## Qué NO contiene, a propósito

Nada privado: ninguna llave, ningún nombre, correo o teléfono de un propietario, nada del
registro de propiedades de nadie, ninguna pregunta que alguien haya hecho.

Y tres entradas quedan fuera del paquete por completo:

- **`mined-459` y `mined-400`** — los criterios de screening (4x ingresos, score 700, 3x la
  renta). Publicar los criterios de **una** empresa a un desconocido como si fueran el
  estándar legal es cómo empieza una queja por impacto dispar. `guardrails.py` tiene un
  rechazo fijo para este tema precisamente por eso, y Grok no lo tiene.
- **`mined-337`** — dice que el aviso de 5 días es el prerrequisito legal para *cualquier*
  desalojo en Illinois. Es cierto para falta de pago y falso para los demás motivos.
  Reetiquetarla no arregla una frase que está mal como ley.

Otras 22 entradas sí van, pero marcadas **"GC Realty policy, not law"** y en su propia
sección: son cosas como el límite de 14 días para visitas o quién cambia los focos. Son
reglas de tu casa, no ordenanzas, y llegaban etiquetadas como "Chicago" o "Illinois" igual
que todo lo demás. El exportador las detecta por la URL de origen (`/resident-handbook`,
`/owner-handbook`, `/leasing-faq`, etc.), así que cualquier regla que mines mañana de esas
páginas queda marcada sola.

## Dos cosas que deberías decidir

**1. `never_say` está vacío en `sources.yaml`.** En una superficie sin guardrails es el
único freno que el paquete puede llevar, y ahora mismo no lleva ninguno. Como mínimo valdría
la pena poner: no cotizar un honorario de administración, y no recomendar a un abogado por
nombre. El comando te lo recuerda cada vez que corre.

**2. Esto es lo más valioso del repo.** 544 hechos revisados, citados y con fuente — el
trabajo de meses de minería y revisión. Un chatbot de terceros guarda lo que le subes y
puede entrenar con ello. No es una razón para no hacerlo, pero sí para decidirlo a
sabiendas en vez de por omisión.

## Mantenerlo al día

Ninguna entrada en `facts.yaml` tiene fecha de vigencia — ni una sola de las 446 ordenanzas
tiene `effective_from`. El paquete lleva la fecha de exportación y le dice al bot que las
ordenanzas suelen cambiar el 1 de enero, pero eso es una advertencia, no una solución.

Vuelve a correr `python -m markai export` cada vez que aceptes hechos nuevos con
`mark facts review`, y en todo caso antes del **1 de marzo** de cada año.
