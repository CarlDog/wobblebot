# Developer command-line tools

Gitleaks is required by the active pre-commit hook. Install a standalone binary
on `PATH`; commits must not bypass the identity, secret or PII checks. Use `rg`
for text searches, `fd` for file discovery, `jq` for JSON and `yq` for YAML.
`bat` and `delta` are optional display tools. Use standalone installations rather
than adding a versioned editor-internal directory to `PATH`.

Python/package and Docker prerequisites are in the README and
[reproducible-build guide](docs/implementation/reproducible-builds.md). This repo
uses GitHub CI, not Azure DevOps; `az` plus its `azure-devops` extension is relevant
only if an Azure remote/CI is deliberately adopted. No login or tool installation
is performed by this document.

Verify local availability with `gitleaks version`, `rg --version`, `fd --version`,
`jq --version` and `yq --version`. Windows terminals need a new session after a
PATH installation. Formatter/linter versions come from `requirements-dev.lock`,
not independently installed global executables.
