# Startup-bound health database readers

The HTTP health surfaces receive a `HealthDatabaseReader` created once by the
application factory. Its parameterless `read()` method returns daemon freshness
and LLM call streaks; request handlers cannot supply a database path. The factory
captures observe, advise and operator database selections and the immutable health
thresholds before serving requests. Changing the later WebConfig object or its
FastAPI dependency does not reconfigure this reader.

## Permitted files and lifecycle

Operator-configured relative and external absolute paths remain supported. Each
path resolves once at startup, including a configured symlink. The reader uses
that canonical target afterward, so retargeting the original alias cannot redirect
health reads. Each target must still resolve to the captured canonical path and
be a regular file with the pinned `(device, inode)` identity. No size or modification
time is pinned: ordinary SQLite writes, WAL updates and VACUUM remain valid.

An absent database is unknown and is never created by the reader. It may appear
later at the configured target; the first valid regular file is pinned before any
await. Permission failures produce unknown/unavailable observations. Once pinned,
a replacement file is not silently accepted. Restart the web application after
an intentional database replacement/restore or path change to bind the reviewed
files again. A startup path-resolution failure also needs a restart after repair.

Both freshness and streak queries use SQLite `mode=ro`, with URI-escaped filenames.
They neither migrate schemas nor deliberately write application data. Read-only
WAL access still depends on the deployed SQLite/filesystem sidecar permissions;
the existing isolated-deployment rehearsal and NAS validation remain required.

## Defense and limits

Grants are checked before reading and again after both query groups. A detected
identity change discards the entire observation as unknown/unavailable. This
strengthens path-selection authority at the application boundary; it is not merely
a taint annotation, filename filter or scanner suppression.

These checks are not an OS sandbox. Path validation and SQLite opening are separate
operations. A malicious filesystem writer could swap a target or its parents
between checks and restore them afterward; inode reuse is also outside this
guarantee. Protect database directories and WAL/SHM sidecars with the existing
mount/OS permissions. Configuration editors retain their intentional startup file
selection authority; arbitrary Python execution or replacement of app state is
not an HTTP capability this reader can sandbox. No NAS ACL requirement is waived.

Tests cover custom escaped filenames, positive reads, nonexistent-file noncreation,
late appearance/concurrent first reads, replacement rejection/restart, configured
alias retargeting, canonical symlink replacement, permission failures, WAL/VACUUM,
mid-read replacement and attempted HTTP/configuration substitution. Mutation tests
remove individual guards to establish that the regressions fail. The no-write
byte comparison is a non-WAL fixture, not a blanket claim about sidecar behavior.
Fresh hosted CodeQL and platform checks remain separate verification evidence.
