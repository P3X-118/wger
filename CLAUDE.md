# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

This is **wger** — a Django-based FLOSS workout, fitness, nutrition and weight
manager/tracker (upstream: <https://github.com/wger-project/wger>). This checkout
is the **`P3X-118/wger` fork** (`origin` = `git@github.com:P3X-118/wger.git`),
being brought into the **SGC self-hosting ecosystem** (`~/sgc`). The end goal is
to build and publish our own Docker image to **`legitservices/wger`** and wire
wger into the SGC Ansible playbook as a first-class service.

The broader ecosystem this feeds into is documented in **`~/sgc/CLAUDE.md`** and
**`~/sgc/AGENTS.md`** — read those before doing any SGC-side integration work
(creating the `wger-ar` role, editing `~/sgc/SGC/requirements.yml`, etc.).

> Stock wger upstream docs: <https://wger.readthedocs.io>. The official compose
> deployment lives in a *separate* repo (`wger-project/docker`); this repo is the
> application source + the image Dockerfiles under `extras/docker/`.

## SGC fork & release model (READ FIRST)

We apply the SGC **3-branch strategy** to this source fork. Mirror the same model
the `-ar` ansible roles use:

| Branch | Role | Editable? |
|---|---|---|
| `master` | **Read-only upstream mirror** of `wger-project/wger` (upstream's default branch is `master`). Synced from upstream; never commit SGC changes here. | No — sync only |
| `sgc-dev` | **Security-review + cherry-pick staging.** Upstream changes that land on `master` are reviewed here and selectively cherry-picked. SGC modifications are developed/tested here. | Yes |
| `sgc` | **Production release branch.** The `legitservices/wger` image is built from here. Tagged releases only. | Merge from `sgc-dev` + tag |

- Only `master` exists today; **`sgc-dev` and `sgc` are to be created** as part of
  this integration. There is currently **no `upstream` remote** — add one to sync:
  `git remote add upstream https://github.com/wger-project/wger.git`.
- **Flow:** upstream → sync to `master` → review/cherry-pick to `sgc-dev` → test →
  merge to `sgc` → tag → build & push image → bump version in the SGC role's
  `requirements.yml`.
- **The point of `sgc-dev` is security review.** Upstream merges to `master` are
  not trusted wholesale; vet diffs there and cherry-pick deliberately rather than
  fast-forwarding everything into production.
- **Version / release tags:** the canonical version is `VERSION = Version('…')` in
  [`wger/version.py`](wger/version.py) (currently `2.6.0`). `.github/workflows/docker.yml`
  extracts this string *verbatim* to tag the image, so it must stay
  `[A-Za-z0-9_.-]`. SGC production tags append an incrementing `-N` suffix to the
  upstream version, exactly like the roles: e.g. `2.6.0-0`, `2.6.0-1`, …
  (`package.json` may say `2.6-alpha1`; `wger/version.py` is the source of truth
  for image tags.)

## Building & releasing the `legitservices/wger` image (the mission)

The image is a **two-stage chain** — there is a shared **base image** that the
production image's `FROM` references, so it must exist locally/in-registry first.
All builds **must run from the repository root** (the Dockerfiles `COPY` from `.`).

```bash
# 1. Build the shared base image (Ubuntu 24.04 + system deps; tag MUST be wger/base:latest
#    because extras/docker/production/Dockerfile does `FROM wger/base:latest`).
docker build -f extras/docker/base/Dockerfile -t wger/base:latest .

# 2. Build the production (server) image, tagged for our registry.
docker build -f extras/docker/production/Dockerfile \
  -t docker.io/legitservices/wger:2.6.0-0 \
  --build-arg BUILD_COMMIT="$(git rev-parse HEAD)" \
  --build-arg BUILD_DATE="$(date -u +%Y-%m-%d)" .

# 3. Push (DockerHub `legitservices` namespace — same target as every other SGC image).
docker push docker.io/legitservices/wger:2.6.0-0
```

This mirrors the manual build/push convention used elsewhere in SGC (e.g.
`~/sgc/apps/pds-signup/justfile`'s `image` target). Consider adding an equivalent
`justfile`/`image` target to this fork on the `sgc` branch.

**Production Dockerfile** ([`extras/docker/production/Dockerfile`](extras/docker/production/Dockerfile))
is multi-stage:
- *builder*: installs Node 22 + build toolchain, builds Python wheels
  (`pip3 wheel --group docker .` — the `docker` group adds `gunicorn`), compiles
  SCSS (`npm run build:css:sass`), and hand-extracts the
  `@wger-project/react-components` npm tarball into `node_modules`.
- *final*: `FROM wger/base:latest`, non-root user `wger`, `WORKDIR /home/wger/src`,
  `EXPOSE 8000`, `ENV DJANGO_SETTINGS_MODULE=settings.main`, installs the wheels,
  runs `compilemessages`, `CMD ["/home/wger/entrypoint.sh"]`.

**Entrypoint behavior** ([`extras/docker/production/entrypoint.sh`](extras/docker/production/entrypoint.sh)) —
runs on every container start, gated by env vars:
1. `wger bootstrap --no-process-static` (migrations, load fixtures, ensure admin user)
2. `collectstatic` if `DJANGO_DEBUG=False` and `DJANGO_COLLECTSTATIC_ON_STARTUP!=False`
3. `migrate` if `DJANGO_PERFORM_MIGRATIONS=True`
4. optional `sync-exercises` / `download-exercise-images` / `download-exercise-videos` /
   `load-online-fixtures` (each behind a `*_ON_STARTUP` flag; `SYNC_INGREDIENTS_ON_STARTUP`
   is intentionally rejected — too slow, run manually)
5. `set-site-url`, then serve: **gunicorn** (`wger.wsgi:application --preload`) if
   `WGER_USE_GUNICORN=True`, else the Django dev server, on `${WGER_PORT:-8000}`.

**Celery** runs as *separate containers* off the same image, overriding the command
with the bundled scripts: `/start-worker`, `/start-beat`, `/start-flower`. An SGC
deployment that enables async work (`USE_CELERY=True`) needs worker + beat services
alongside the web service, plus Redis.

**Upstream CI** ([`.github/workflows/docker.yml`](.github/workflows/docker.yml)) builds
multi-arch (`amd64`+`arm64`) and pushes to `${{ vars.REGISTRY_REPO }}/server` using
`DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN`. To retarget this to `legitservices/wger` you
must change the `REGISTRY_IMAGE` env (the hardcoded `/server` suffix → `/wger`) and
set the repo variable/secrets — otherwise prefer the manual build above. There are
also `docker-base.yml` (base image) and `docker-demo.yml` (Apache-based demo image)
workflows.

## Development

Python deps are managed with **`uv`** (`uv.lock`, `pyproject.toml` with
`[dependency-groups]`); frontend assets with **npm**.

```bash
# Install Python deps (+ dev tools: ruff, isort, coverage, faker, debug-toolbar…)
uv sync --group dev

# Frontend: install JS deps and compile the Bootstrap SCSS theme
npm install
npm run build:css:sass        # → wger/core/static/bootstrap-compiled.css

# Local dev bootstrap + server (SQLite, DEBUG=True; see settings/local_dev.py)
export DJANGO_SETTINGS_MODULE=settings.local_dev
uv run ./manage.py migrate
uv run wger load-fixtures             # exercises/ingredients/gym fixtures
uv run wger create-or-reset-admin     # admin / adminadmin
uv run ./manage.py runserver          # or: uv run wger start --port 8000
```

The **`wger` CLI** (`pyproject.toml` `[project.scripts]`, implemented via Invoke in
[`wger/tasks.py`](wger/tasks.py)) wraps common ops: `bootstrap`, `migrate-db`,
`load-fixtures`, `load-online-fixtures`, `create-or-reset-admin`, `start`. Custom
Django management commands worth knowing: `sync-exercises`, `sync-ingredients`,
`download-exercise-images`, `download-exercise-videos`, `set-site-url`.

### Tests

Tests are Django `TestCase`-based and run under the **`settings.ci`** module (NOT
`settings.main`, which expects a real DB/env). CI uses an in-memory SQLite DB with
migrations skipped for speed.

```bash
mkdir -p /tmp/wger-test    # DJANGO_MEDIA_ROOT used by the suite

# Full suite (parallelized, as in CI)
DJANGO_SETTINGS_MODULE=settings.ci DJANGO_MEDIA_ROOT=/tmp/wger-test \
  uv run ./manage.py test --parallel auto

# A single module (or narrow to .<TestCase>.<test_method>); drop --parallel for a focused run
DJANGO_SETTINGS_MODULE=settings.ci DJANGO_MEDIA_ROOT=/tmp/wger-test \
  uv run ./manage.py test wger.core.tests.test_change_password

# With coverage (config in .coveragerc; omits migrations/ and settings/)
uv run coverage run --source='.' ./manage.py test --parallel auto && uv run coverage lcov
```

### Lint & format

`ruff` (lint + format) and `isort` (imports). Config is in
[`pyproject.toml`](pyproject.toml) — **ruff line-length 100, single-quote style**;
**isort line-length 119** with custom `FUTURE/STDLIB/DJANGO/THIRDPARTY/FIRSTPARTY/LOCALFOLDER`
sections (mismatched line-lengths are intentional). There is no pre-commit config.

```bash
uv run ruff check .            # lint
uv run ruff format --check .   # format check (CI)   — `ruff format .` to apply
uv run isort --check-only .    # import check (CI)    — `isort .` to apply
```

> Heads-up: upstream's `formatter.yml` GitHub Action **auto-commits** `ruff format`
> + `isort .` results as "Automatic linting" on pushes to `master` that touch
> `**.py`. If that workflow is active on the fork it will rewrite formatting; keep
> code pre-formatted to avoid surprise commits / conflicts during upstream syncs.

## Architecture (big picture)

### Layered settings — the configuration backbone

Everything deployment-related flows through the settings layering; understand this
before touching config:

- [`settings/settings_global.py`](settings/settings_global.py) — defaults shared by
  all environments: `INSTALLED_APPS`, middleware, DRF/JWT/allauth config, the
  `WGER_SETTINGS` feature-toggle dict, default LocMem cache.
- [`settings/main.py`](settings/main.py) — **production** overrides, **driven entirely
  by environment variables via `django-environ`**. This is the contract the SGC
  ansible role/env file must satisfy. `DJANGO_SETTINGS_MODULE=settings.main` is the
  Docker default.
- [`settings/local_dev.py`](settings/local_dev.py) — dev (DEBUG, SQLite, console email,
  Celery off). [`settings/ci.py`](settings/ci.py) — tests.
- `manage.py` defaults to `settings.main`; override via `DJANGO_SETTINGS_MODULE`.

### Django apps (`wger/<app>/`)

`core` (auth, user profiles, languages/licenses — the central app) and `utils`
(shared base models, permissions, pagination, middleware, PDF, PowerSync plumbing)
are the foundation. Feature apps: `manager` (workout routines/logs — the heart of
the app), `exercises` (exercise DB + sync from wger.de), `nutrition` (ingredients,
plans, Open Food Facts integration + sync), `weight`, `measurements`, `gym` (multi-user
gym management), `gallery`, `trophies`, `config`, `mailer`, `software`. Each app
typically holds `models/`, `api/` (`views.py`/`serializers.py`/`filtersets.py`),
`templates/`, `migrations/`, `management/commands/`, `tasks.py`, `tests/`.

Models lean on `django-simple-history` (audit trails → `Historical*` shadow tables),
`django-sortedm2m` (ordered relations), and `django-activity-stream`.

### API

DRF + **drf-spectacular** (OpenAPI). Routes registered in
[`wger/urls.py`](wger/urls.py) under **`/api/v2/`**; schema/docs at
`/api/v2/schema`, `/api/v2/schema/ui`, `/api/v2/schema/redoc`. Auth: session,
legacy `Token`, and **JWT (RS256 via simplejwt)** for the mobile/SDK clients — JWT
requires the `JWT_PRIVATE_KEY`/`JWT_PUBLIC_KEY` env keypair. `django-allauth`
provides social login + MFA (TOTP/recovery/WebAuthn) and a headless flow for the
Flutter app. `django-axes` does brute-force lockout.

### Background work (Celery + Redis)

Optional, gated by `USE_CELERY`. Config in
[`wger/celery_configuration.py`](wger/celery_configuration.py); tasks live in each
app's `tasks.py`. Main jobs: sync exercises/images/videos from the upstream wger
instance (`WGER_INSTANCE`, default `https://wger.de`), sync ingredients + Open Food
Facts daily deltas, cache API exercise payloads, expire JWT tokens/sessions. Several
have randomized periodic schedules behind `*_CELERY` flags. Redis also backs the
cache and (in prod) the session store.

### Frontend

Server-rendered **Django templates + crispy-forms (Bootstrap 5)** with **htmx**,
jQuery and DataTables for interactivity, plus the published
**`@wger-project/react-components`** npm package for newer UI. The Flutter mobile
app and third-party integrations consume the REST API. Static assets are gathered
with `collectstatic` into `DJANGO_STATIC_ROOT`.

## Deployment configuration (env vars the SGC role must provide)

`settings/main.py` is the authoritative list; the essentials for an SGC deployment:

| Concern | Env vars |
|---|---|
| **Database** | `PS_DATABASE_URI` (single Postgres URI, preferred) **or** `DJANGO_DB_ENGINE`/`DJANGO_DB_DATABASE`/`DJANGO_DB_USER`/`DJANGO_DB_PASSWORD`/`DJANGO_DB_HOST`/`DJANGO_DB_PORT` (falls back to SQLite at `/home/wger/db/database.sqlite`) |
| **Secrets** | `SECRET_KEY` (auto-generated if unset in prod — set it explicitly so sessions survive restarts), `JWT_PRIVATE_KEY` + `JWT_PUBLIC_KEY` (RS256; required for JWT auth when `DEBUG=False`) |
| **Cache / Redis** | `DJANGO_CACHE_BACKEND` (e.g. `django_redis.cache.RedisCache`), `DJANGO_CACHE_LOCATION`, `DJANGO_CACHE_CLIENT_CLASS`, optional `DJANGO_CACHE_CLIENT_PASSWORD`/SSL keys |
| **Celery** | `USE_CELERY`, `CELERY_BROKER`, `CELERY_BACKEND` (default `redis://cache:6379/2`) |
| **Web / proxy** | `DJANGO_DEBUG` (=`False`), `SITE_URL`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `WGER_USE_GUNICORN=True`, `WGER_PORT`, `DJANGO_PERFORM_MIGRATIONS`, `DJANGO_COLLECTSTATIC_ON_STARTUP` |
| **Files** | `DJANGO_MEDIA_ROOT` (`/home/wger/media`), `DJANGO_STATIC_ROOT` (`/home/wger/static`); optional S3 via `USE_S3_MEDIA_FILES`/`USE_S3_STATIC_FILES` + `AWS_*` |
| **Email** | `ENABLE_EMAIL=True` gate, then `EMAIL_HOST`/`EMAIL_PORT`/`EMAIL_HOST_USER`/`EMAIL_HOST_PASSWORD`/`EMAIL_USE_TLS`, `FROM_EMAIL` |
| **Features** | `ALLOW_REGISTRATION`, `ALLOW_GUEST_USERS`, `USE_RECAPTCHA`+`RECAPTCHA_*`, `WGER_SOCIAL_PROVIDERS`, `EXPOSE_PROMETHEUS_METRICS`, the `SYNC_*_CELERY` toggles, `AUTH_PROXY_*` (reverse-proxy SSO) |

**Persistent volumes** (from the image's WORKDIR/user `wger`, home `/home/wger`):
`media/`, `static/`, `beat/` (celerybeat schedule), `db/` (only if SQLite). A
production SGC deployment uses external Postgres + Redis, so only `media/` (and
`static/` unless served from S3/Traefik) truly need persisting.

## SGC integration checklist (work happens in `~/sgc`, not here)

When wiring wger into the playbook, follow the 4-step "add a role" pattern in
`~/sgc/CLAUDE.md` / `~/sgc/AGENTS.md`. wger is a **Postgres-backed web service**, so
it follows the postgres patterns (the `awx` role is the closest existing template —
also a Postgres-backed Django app pulled from `legitservices/`):

1. Create the role at `~/sgc/ansible/roles/wger-ar/` (defaults, tasks, `templates/env.j2`,
   `templates/labels.j2`, `templates/systemd/*.service.j2` — systemd units that
   `docker create`/`start` the `legitservices/wger` image; **not** docker-compose).
2. Add to `~/sgc/SGC/requirements.yml` (`git+https://github.com/P3X-118/wger-ar.git`,
   `version: 2.6.0-0`, `name: wger`, `activation_prefix: wger_`).
3. Add to `~/sgc/SGC/setup.yml` and `group_vars/mash_servers`
   (`sgc_sysd_srvc_list_auto_itemized`, `postgres_autom_itemized`, and the service
   vars block) — all wrapped in `# role-specific:wger … # /role-specific:wger` markers.
4. Derive secrets from `sgc_pgsk` (`db.wger`, `wger.secret_key`, etc.) and wire the
   `JWT_*` keypair, Redis cache, Traefik labels, and Celery worker/beat services.

> Note: web + celery-worker + celery-beat are three systemd services off one image —
> account for all three in the role's service list and Traefik labels.
