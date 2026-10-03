# Reproducible dependencies and runtime identity

The package manager remains pip. `requirements.lock` is the universal hashed
runtime resolution for the supported Python 3.13/3.14 targets;
`requirements-build.lock` pins wheel build tools; `requirements-dev.lock` pins
runtime plus test/tooling and build tools. Install with `make install`, or:

```sh
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation -e .
```

For a deliberate dependency refresh, resolve all three together, review version
and license changes, then verify the full supported matrix. The resolver used
here is `uv pip compile`, not a migration to uv project/package management:

```sh
uv pip compile pyproject.toml --universal --generate-hashes --python-version 3.13 --output-file requirements.lock
uv pip compile requirements-build.in --universal --generate-hashes --python-version 3.13 --output-file requirements-build.lock
uv pip compile pyproject.toml requirements-build.in --extra dev --universal --generate-hashes --python-version 3.13 --output-file requirements-dev.lock
```

The Dockerfile pins the Python base by digest, verifies runtime/build hashes,
requires runtime binary wheels and builds the application with locked build tools.
It no longer installs a floating compiler through apt. A missing wheel fails the
build; do not relax hashes or switch to an unreviewed source build. The runtime
installs offline from verified wheels and runs `pip check`. It remains non-root.
For a TLS-inspecting build network, the optional BuildKit `build_ca` secret may
supply the approved CA bundle; it is ephemeral, not an image layer. Never disable
TLS verification. Proxy DNS must be configured by the build environment.

Pass `--build-arg VCS_REF=<full-verified-commit>` for a clean source build. For a
working tree with uncommitted changes, leave revision `unknown`; do not label it
with a fabricated or misleading clean commit. Published CI retains revision,
image digest and runtime lock hash in the image-identity artifact. This is an
artifact from a future authorized CI run, not evidence that it has run locally.

Use the isolation generator's `--image registry/repository@sha256:<digest>` to
prepare digest-pinned service definitions after inspecting the built/published
image. A tag alone is rejected for this option. The generator records the digest
as a **declared** runtime identity; the container cannot independently attest it
without elevated daemon access. No Docker socket is granted. Retain the actual
image inspection receipt with a separately authorized deployment.

Configuration loading logs only a bounded identity record: validated revision,
declared image digest, runtime lock hash, selected profile, resolved-config hash,
combined configured prompt/heuristic hashes and missing-asset count. No raw config,
secret, prompt, header or environment dump is emitted. Config and prompt edits
change fingerprints. Image inspection and permission checks remain necessary.

CI separates quality from a Linux/Windows × Python 3.13/3.14 test matrix; both gate
publishing. Historical upgrade tags are required. Local Linux results do not prove
Windows or hosted CI. Quality retains the established exact-pinned Black/isort/
pylint/mypy chain: no second lint stack is added solely to turn an audit green.
PY-01 Ruff and the Ruff portion of PY-06 remain a disclosed standards gap, not a
newly accepted exception. CodeQL has no checked-in advanced configuration;
GitHub default-setup/analysis status must be reconciled through authorized remote
access before any completed-analysis claim. Existing UTF-8 text conventions and
.gitattributes remain; no repository-wide encoding rewrite is necessary.
