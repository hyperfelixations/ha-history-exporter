# Security policy

## Supported versions

Security fixes are released for the latest version on PyPI. Update with
`pipx upgrade ha-history-exporter`.

## Reporting a vulnerability

Report a vulnerability privately through GitHub: on the
[Security tab](https://github.com/hyperfelixations/ha-history-exporter/security)
of the repository, choose **Report a vulnerability**. Please do not open a
public issue for it.

A useful report names the version (`hhe --version`), the platform, what an
attacker can do and the steps that show it. Leave out real access tokens,
Home Assistant addresses and exported data; synthetic values are enough.

## Scope

- Handling of the Home Assistant access token: storage, transport, logs,
  error messages.
- The connection to Home Assistant: certificate verification, redirects,
  plain HTTP.
- The archive: integrity of published days, file permissions, the content of
  logs, snapshots and manifests.
- The distributions on PyPI and their release pipeline.

Vulnerabilities of Home Assistant itself belong to the
[Home Assistant project](https://www.home-assistant.io/security/).
