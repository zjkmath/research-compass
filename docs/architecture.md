# Architecture

Django server-rendered pages and session authentication; SQLite for the default instance; Waitress WSGI and WhiteNoise collected static resources. No frontend build platform or external paid API is required.

`radar/settings.py` controls environment boundaries. `compass.py` manages a loopback local instance with an explicit external state directory and persistent key. `pilot.py` provides private loopback/verified-proxy operation using an external owner-only configuration. `opportunities` contains the data model, reviewed import, discovery state, proposals, maintenance queue, fit, support and decision logic.

The public release replaces maintainer-specific factual release loaders with schema-only upgrades. Real datasets are not a software dependency. Only `data/demo` is distributed; it is entirely invented. Source code for permission-aware interfaces remains available, but source authorization is off by default.
