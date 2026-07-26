# Monitor de salud web — Country Villas Lanzarote

Rastrea el sitio cada 2 horas, detecta enlaces rotos, páginas huérfanas y
caídas (incluso silenciosas, de JS) en la ficha de reserva crítica, y avisa
por correo SOLO cuando hay algo nuevo que reportar.

**Arquitectura, 100% gratis, sin servidor propio:**

| Pieza | Para qué |
|---|---|
| GitHub (repo + Actions) | "Reloj" que lo ejecuta cada 2h, y máquina donde corre |
| Supabase | Guarda el estado entre ejecuciones (qué se sabía la vez anterior) |
| Resend | Envía el correo de aviso |

No hace falta Vercel para esto: su plan gratuito solo permite cron una vez
al día, y aquí necesitamos cada 2 horas. GitHub Actions sí lo permite gratis
(y sin límite de minutos, si el repositorio es público).

---

## Paso 1 — Crear el repositorio en GitHub

1. Entra en https://github.com/new
2. Nombra el repo como quieras (ej. `web-monitor-cvl`).
3. **Marca "Public"** (público). Esto es importante: en un repo público,
   GitHub Actions es gratis SIN LÍMITE de minutos. En uno privado, el
   plan gratuito da 2.000 minutos/mes, y un rastreo completo del sitio
   cada 2 horas (12 veces al día) puede consumirlos rápido.
   - No hay ningún dato sensible en el código: las claves de Resend y
     Supabase van aparte, cifradas, en "Secrets" (paso 4) — nunca en el
     código ni visibles en el repo público.
4. Sube estos archivos a la raíz del repo (o haz `git clone`, copia los
   archivos, `git add . && git commit -m "monitor" && git push`):
   - `site_monitor.py`
   - `requirements.txt`
   - `.github/workflows/monitor.yml`

## Paso 2 — Preparar Supabase (guardar el estado)

1. En tu proyecto de Supabase, abre el **SQL Editor** y ejecuta:

```sql
create table if not exists site_monitor_state (
  id text primary key,
  data jsonb not null,
  updated_at timestamptz default now()
);
```

2. Ve a **Project Settings → API** y copia:
   - **Project URL** (algo como `https://xxxxx.supabase.co`)
   - **service_role key** (¡ojo! no la `anon` key -- la `service_role`,
     que tiene permiso de escritura y hay que guardarla en secreto)

## Paso 3 — Preparar Resend (envío del correo)

1. En https://resend.com/api-keys crea una API key.
2. Decide el remitente/destinatario, hay dos casos:
   - **Caso simple (recomendado para empezar):** si Andrés va a recibir
     el aviso en la MISMA dirección de correo con la que se registró en
     Resend, no hace falta nada más -- se puede enviar ya mismo desde
     `onboarding@resend.dev` a esa dirección.
   - **Caso con dominio propio:** si quieres enviarlo a otra dirección
     (por ejemplo, la del equipo técnico directamente), hay que verificar
     un dominio propio en https://resend.com/domains (gratis, pero
     requiere poder añadir registros DNS de ese dominio). Una vez
     verificado, se puede usar como remitente cualquier@tudominio.com y
     enviar a cualquier destinatario.

## Paso 4 — Configurar los "Secrets" en GitHub

En el repositorio: **Settings → Secrets and variables → Actions → New
repository secret**. Crear estos 5 secrets:

| Nombre | Valor |
|---|---|
| `SUPABASE_URL` | La Project URL de Supabase (paso 2) |
| `SUPABASE_SERVICE_KEY` | La service_role key de Supabase (paso 2) |
| `RESEND_API_KEY` | La API key de Resend (paso 3) |
| `SITE_MONITOR_EMAIL_FROM` | `onboarding@resend.dev` (o tu remitente verificado) |
| `SITE_MONITOR_EMAIL_TO` | El correo de Andrés que debe recibir los avisos |

**Personalizar el remitente:** sin dominio verificado, la dirección tiene
que ser exactamente `onboarding@resend.dev`, pero el nombre visible sí se
puede cambiar: `SITE_MONITOR_EMAIL_FROM = "Monitor CVL <onboarding@resend.dev>"`.
Con dominio verificado, puedes usar cualquier dirección de ese dominio.

**Varios destinatarios:** separa las direcciones por coma en el secret
`SITE_MONITOR_EMAIL_TO`, por ejemplo:
`andres@correo.com,tecnico@correo.com`. El script las trocea y se las pasa
a Resend como lista. Ojo: mientras uses `onboarding@resend.dev` sin
dominio verificado, Resend solo entrega al correo del propio dueño de la
cuenta -- si añades otra dirección distinta, ese envío fallará hasta que
verifiques un dominio propio.

## Paso 5 — Probarlo

1. Ve a la pestaña **Actions** del repo.
2. Selecciona el workflow "Monitor de salud web - Country Villas
   Lanzarote".
3. Pulsa **Run workflow** (ejecución manual, para no esperar a que
   toque el cron).
4. Se tarda varios minutos (rastrea el sitio entero). Al terminar, revisa
   los logs del paso "Ejecutar el monitor": deben decir que se ha creado
   la línea base y, si Resend está bien configurado, que se ha enviado
   un correo.
5. Revisa el correo de Andrés -- debería haber llegado el aviso de
   "primer rastreo, línea base creada".

A partir de aquí, se ejecuta solo cada 2 horas (ver el cron en
`.github/workflows/monitor.yml`), sin que nadie tenga que tocar nada.

---

## Cómo comprobar que sigue funcionando sin mirarlo cada día

- Pestaña **Actions** del repo: se ve el historial de ejecuciones, con
  ✅ o ❌ en cada una.
- Si una ejecución falla (❌), GitHub envía automáticamente un correo al
  dueño del repositorio avisando del fallo del workflow -- así os
  enteraríais igualmente aunque el propio monitor no pudiera avisar.

## Chequeo funcional con navegador real (Playwright)

Como se instala automáticamente en el workflow (`playwright install
--with-deps chromium`), este chequeo del widget de reserva (precios
reales, calendario, peticiones a Avantio) queda activo desde el primer
día sin nada adicional que instalar.

## Páginas críticas (fichas reservables)

La ficha de Casa Duran ya está en la lista `CRITICAL_PAGES` dentro de
`site_monitor.py`. Para añadir más fichas críticas, edita esa lista
directamente en el código y vuelve a subir el cambio (`git push`) --
no hace falta redeploy manual, el siguiente cron ya usará la lista nueva.

## Notas importantes

- El sitio NO tiene `sitemap.xml` ni `robots.txt` (confirmado). Por eso
  la detección de páginas huérfanas es histórica: el script recuerda qué
  páginas ha visto enlazadas en el pasado.
- El script reintenta 2 veces antes de dar un enlace por roto, para
  evitar falsas alarmas por cortes de red puntuales.
- El rastreo ignora URLs con parámetros de búsqueda (`?...`) para no
  quedar atrapado en las combinaciones del buscador de fechas/huéspedes
  del motor de reservas (Avantio).
- Un rastreo completo del sitio puede tardar bastantes minutos (cientos
  de páginas comprobadas una a una, con pausas de cortesía entre
  peticiones). Esto es normal y está dentro del límite de tiempo del
  workflow (45 minutos). Si algún día se quedara corto, se puede subir el
  valor `timeout-minutes` en `monitor.yml`.
- Todo el histórico y la comparación entre ejecuciones vive en la tabla
  `site_monitor_state` de Supabase -- no se debe borrar esa fila, o se
  perderá la comparación y el próximo aviso tratará todo como "primera
  vez".
