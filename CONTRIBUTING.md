# Contributing

Keep changes small and limited to the existing evidence/decision workflow. Explain the problem, the behavior change and relevant verification. Do not add real mentor/opportunity datasets, page dumps, copyrighted figures or private operational artifacts.

Use the locked dependencies and the README verification commands. Tests use fictional records and mocked network responses; never contact production or crawl sources in CI. New migrations must preserve private favorites, notes, budgets, decisions and history. Unknown/conflict states must not be changed merely to make a test pass.

Run a secret scanner on both files and Git history before submitting. Do not submit `.env`, keys, databases, personal profiles, production addresses or acceptance materials. Use `example.invalid` for fictional URLs and generate test credentials only at runtime.

Contributions are submitted under Apache-2.0. By submitting a contribution you confirm you have the rights to license it and have identified any applicable third-party license. Keep upstream notices intact. Source corrections must identify the field, bounded scope and a public source without copying a whole page or personal correspondence.

Security problems must be reported privately through the process in SECURITY.md.
