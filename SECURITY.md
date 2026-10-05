# Security policy

## Supported releases

Security fixes are supported for the latest 1.x release. Older releases must be upgraded; there is no guarantee of long-term support.

## Reporting a vulnerability

Use **Security → Advisories → Report a vulnerability** in this repository when private vulnerability reporting is enabled. Do not publish an exploit containing credentials or personal data. If the button is unavailable, open a public issue containing only “Private reporting channel requested”, with no vulnerability details, logs or attachments; wait for a private reporting channel.

Include affected version, impact and minimal reproduction using synthetic data. Remove cookies, tokens, database contents, hostnames identifying private infrastructure, passwords, email addresses and personal application materials. The maintainers will triage reports as available; no fixed response-time or bounty commitment is made.

## Operating safely

Keep databases, persistent keys, secrets and configuration outside the source checkout and outside public web roots. Use exact Host/CSRF origins, HTTPS with a correctly configured trusted proxy, secure cookies, access controls and consistent backups. The local launcher binds only to loopback. Disable debug in production. Source header strings never authenticate a Django user. No automated collection permission or scheduler is supplied by the demo.

Dependency alerts and secret scanning reduce risk; a passing scan does not prove every future commit is safe. Rotate any exposed secret immediately, then remove it from history. Do not use push-protection bypasses to publish secrets.
