# 研途 · Research Compass

Evidence-aware research opportunity and mentor decision workspace for academic mobility.

Research Compass is a Django application for reviewing research fit, formal visiting routes, opportunity evidence, support conditions, comparisons, budgets and private follow-up. Server-rendered Chinese pages keep unknown, stale and conflicting facts visible rather than inventing a single mentor score.

**This public source release contains only synthetic demo data.** Every seeded institution, person, opportunity, date, amount, ranking and evidence statement is fictional. Demo links use reserved `.invalid` domains and deliberately do not resolve. Demo state labels such as “verified” demonstrate software behavior, not real-world verification. Live research datasets, source excerpts, private profiles, production configuration and acceptance materials are excluded. No demo account or password is shipped.

## Quick start

Python 3.10 or 3.13 and Git are supported by CI. SQLite is the default. From a clean clone:

```sh
python -m venv .venv
```

Activate it: Windows PowerShell `./.venv/Scripts/Activate.ps1`; Linux/macOS `source .venv/bin/activate`.

```sh
python -m pip install -r requirements.lock
python manage.py collectstatic --noinput
python compass.py --data-dir ../research-compass-state init
python compass.py --data-dir ../research-compass-state manage createsuperuser
python compass.py --data-dir ../research-compass-state --no-browser start
```

Open **http://127.0.0.1:8765/** and sign in with the account you just created. Keep the state directory outside the repository. The launcher binds to loopback only. An existing database is never reseeded by `init`; use `upgrade` for schema upgrades. This OSS version does not automatically import maintenance-release facts.

```sh
python compass.py --data-dir ../research-compass-state stop
python compass.py --data-dir ../research-compass-state upgrade
python compass.py --data-dir ../research-compass-state backup ../backup.sqlite3
python compass.py --data-dir ../research-compass-restored restore ../backup.sqlite3 ../research-compass-state/dev_key
```

Keep the original persistent `dev_key` with a private backup. Do not commit it. Stop your own instance before upgrading or restoring. See [deployment and backup](docs/deployment.md).

## Verify a clean build

```sh
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test --noinput
python tools/rebuild_smoke.py
```

The smoke script creates a temporary synthetic instance, checks initialization, schema-only upgrade, persistent key, SQLite backup/restore, actual restored HTTP login/read/write, restart and static resources; it stops its own services. It does not contact live sources. Browser rendering is separately reviewed; an HTTP smoke test is not a mobile or browser claim.

## Evidence and scope

- Tier A is direct physiological/neuroscience fit; Tier B is method extension. Topic keywords alone establish neither.
- Coverage, actionable contact and deep dossier are different layers. A directory entry is not a current recruitment invitation.
- Institutional policy, current program window, host eligibility and an individual's invitation or award are separate facts. CSC/Yuanhang labels in the demo are fictional policy scenarios.
- Unknown is not false. A reachable page is not evidence that an expired opportunity remains open. Fetch, content-observation, publication and human-verification times are separate.
- A project grant total is not visitor salary. Compare salary/stipend only with known currency, period, tax and applicable work basis; unknown amounts are not zero. Budgets are user assumptions, not financial guarantees.
- Alumni first-destination coverage displays a finite public denominator, not a success rate. Lineage must be sourced, not inferred from coauthorship.
- The demo Priority-20 scope is twenty fictional institutions. It does not reproduce the maintainer's real institution list or imply exhaustive worldwide coverage. Scope guards preserve existing history while restricting new discovery.

Reviewed import commands and permission-aware adapters are source code only. No live source policy, crawling permission or automatic schedule is enabled by the demo. Configure and review your own sources under their terms before use. No external paid API, email sending or application submission is implemented. See [maintenance](docs/maintenance.md), [data model](docs/data-model.md) and [privacy](docs/privacy.md).

## License and contributing

Original source and interface assets: [Apache-2.0](LICENSE). Dependencies retain their own licenses: [third-party notices](THIRD_PARTY_NOTICES.md). No wallpaper, downloaded font, institutional logo or third-party content corpus is shipped.

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR and [SECURITY.md](SECURITY.md) for private vulnerability reports. Never attach your database, credentials, production configuration, private records or personal application materials to a public issue. This software and synthetic data do not guarantee admission, funding or current opportunity availability.
