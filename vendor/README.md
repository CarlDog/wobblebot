# Vendored operator tools

Third-party tools in this directory are optional operator conveniences, not WobbleBot runtime
dependencies. Each tool remains in its own git submodule so its license, history, and update
boundary stay explicit.

## Atlas Cloud CLI

`atlascloud-cli` points to the official
[`AtlasCloudAI/cli`](https://github.com/AtlasCloudAI/cli) repository. WobbleBot does not import or
invoke it; the production Atlas Cloud advisor path calls the API directly through
`OpenAIAdvisorAdapter`.

The gitlink pins the reviewed wrapper/documentation snapshot, not a downloaded executable. The
current gitlink is `b0b8b295688a3b5cdad7c1ab38087018b260f4f9`, a post-`v0.1.16` catalog-sync
commit. The bundled installers default to the latest GitHub release, so running them without an
explicit version is a separate mutable supply-chain decision and is not made safe by the gitlink.

Operator rules:

1. Prefer an explicit installer version (`--version=0.1.16`, `ATLAS_VERSION=0.1.16`, or
   `-Version 0.1.16`, depending on shell) and retain the installer's checksum verification.
2. Never place an Atlas API key, expanded environment output, or command receipt containing a key
   in this repository.
3. Update the gitlink only in an explicit review that checks the upstream diff, license, installer
   behavior, and release/checksum path. Update this note if the trust model changes.
4. Treat the CLI as a manual diagnostic. Do not make WobbleBot services shell out to it; runtime
   integration stays behind the existing adapter/port boundary.
