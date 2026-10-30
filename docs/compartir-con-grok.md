# Cómo darle a Grok el trasfondo de GC Realty

El objetivo es que Grok sepa **quién es GC Realty y qué hacen** — no que conteste preguntas
legales. Para eso está `mark export`.

## El comando

En `cmd`, dentro de `C:\Users\javid\MarkAI`:

```cmd
python -m markai export
```

Escribe `gc-background.md` en tu carpeta `data\`: todo lo que GC Realty ha publicado — las
páginas del sitio, los handbooks, cada post del blog — completo y en sus propias palabras.
Nada resumido, nada reescrito. Si una frase está en el archivo, alguien de GC Realty la
escribió.

Si es muy grande se parte solo, y **siempre corta entre posts, nunca a la mitad de uno**.
Cada parte dice cuál es y dónde quedó el encabezado.

```cmd
python -m markai export --episodes          :: + transcripciones del podcast y YouTube
python -m markai export --out docs\grok     :: en otra carpeta
python -m markai export --part-chars 200000 :: partes más chicas
```

## Lo que incluye y lo que no

**Incluye, por defecto:** solo material de `gcrealtyinc.com` y
`straightupchicagoinvestor.com`. Las páginas del condado, de HUD y de Illinois quedan fuera
— Jay las lee para las ordenanzas, pero no son quiénes son ustedes. `--everything` las
vuelve a meter.

**Las transcripciones quedan fuera por defecto** (`--episodes` las agrega). Son la mayor
parte de las palabras y traen una advertencia que el paquete explica: una transcripción no
registra quién habla, y casi todos los episodios tienen co-host e invitado. "El episodio 311
cubre esto" es seguro; "Mark dijo esto" normalmente no.

**La plantilla del sitio se quita una sola vez.** Un crawl de cuatrocientas páginas arrastra
el mismo menú y el mismo pie de página cuatrocientas veces. Sin quitarlo, eso sería la mayor
parte del archivo y todo ruido. Cualquier línea que aparezca en buena parte de las páginas se
descarta una vez, globalmente, y el paquete dice cuántas líneas se fueron. En un sitio chico
(menos de 12 páginas) no quita nada: con seis páginas, una frase compartida pasaría cualquier
umbral y se comería tu propio contenido.

## Cargarlo en Grok

Súbelo como archivo adjunto. Si es más de una parte, súbelas todas — Grok acepta hasta ~100
archivos en un mensaje y hasta 48 MB cada uno, así que el tamaño no va a ser el problema.

Si usas **Workspaces / Projects**, crea uno llamado *GC Realty* y sube los archivos ahí. Así
quedan adjuntos a cada conversación de ese workspace y no los vuelves a subir.

No necesitas instrucciones especiales para esto. El paquete trae su propio encabezado que le
dice al modelo qué está leyendo, incluida la parte honesta: que es el material de una empresa
de administración, no una fuente neutral, y que varios posts tienen años encima.

## Si además quieres que conteste preguntas

Eso es otro trabajo y necesita otra cosa:

```cmd
python -m markai export --facts
```

Agrega dos archivos más: la capa de hechos revisada (544 entradas con su cita y su fuente) y
un bloque corto de reglas para las **Custom Instructions** de Grok.

Ese bloque **no es opcional**. El trasfondo se puede leer suelto y no pasa nada; la capa de
hechos contesta "cuántos días tengo", y leerla suelta significa un propietario sirviendo el
plazo de Evanston en un contrato de Chicago. Jay nunca ve las 544 entradas de golpe —
`select()` le pone ocho frente a cada pregunta — así que entregarlas todas juntas cambia el
modo de falla de "no tengo el dato" a "agarré el dato de la ciudad equivocada".

**Tus notas internas no van.** Las notas que escribes en una entrada — donde registras que
un post tuyo tiene una cifra al revés, o que el Notice Wizard mostraba el aviso equivocado —
quedan fuera por defecto. Eso no es material para el modelo de un tercero. `--owner-notes`
las incluye si alguna vez las necesitas.

**El comando revisa antes de escribir.** Si un correo o un teléfono se coló en un campo de
`facts.yaml` — y `CostRange.source` es justo donde cae una cotización telefónica de un
proveedor — el export se detiene y te dice en qué entrada y en qué campo está. Las
direcciones solo avisan, porque la de GC Realty aparece citada legítimamente dentro de
`mined-165`. También verifica que el paquete no cite ids que no define.

Tres entradas quedan fuera del paquete de hechos por completo, y el comando te dice cuáles:

- **`mined-459` y `mined-400`** — los criterios de screening. Publicar los criterios de *una*
  empresa como si fueran el estándar legal es cómo empieza una queja por impacto dispar.
- **`mined-337`** — dice que el aviso de 5 días es el prerrequisito para *cualquier* desalojo
  en Illinois. Cierto para falta de pago, falso para los demás motivos.

Otras 22 van marcadas **"GC Realty policy, not law"**, en su propia sección: el límite de 14
días para visitas, quién cambia los focos. Reglas de tu casa que llegaban etiquetadas como
"Chicago" igual que una ordenanza. Se detectan por la URL de origen, así que lo que mines
mañana de `/resident-handbook` queda marcado solo.

## Dos cosas que son tu decisión

**`never_say` está vacío** en `sources.yaml`. Solo importa para el paquete de hechos, donde
es el único freno que viaja. Mínimo sugerido: no cotizar un honorario de administración, y no
recomendar a un abogado por nombre.

**Esto es lo más valioso del repo.** Un chatbot de terceros guarda lo que le subes y puede
entrenar con ello. El paquete de trasfondo es todo contenido ya público, así que ahí el
riesgo es bajo. La capa de hechos no: son meses de minería y revisión tuya, y eso sí lo estás
regalando. No es razón para no hacerlo, pero sí para decidirlo a sabiendas.

## Mantenerlo al día

Vuelve a correr el comando después de cada `mark ingest` o cada `mark facts review`. Ninguna
entrada de `facts.yaml` tiene fecha de vigencia, así que el paquete lleva la fecha de
exportación y avisa que las ordenanzas suelen cambiar el 1 de enero.
