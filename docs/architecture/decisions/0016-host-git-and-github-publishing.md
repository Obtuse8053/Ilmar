# ADR-0016: Plumbing-only host git and GitHub publishing

- Status: Proposed
- Date: 2026-11-04
- Sources: [R17], [R20], [R22] · Findings: F-022, F-026
- Implements: ADR-0006 and ADR-0010 for GitHub (#7)

## Context and problem

ADR-0010 requires:
- agent output frozen as plain file content into a coordinator-owned
  repository;
- hardened host-side git that never runs in an agent-written repository;
- exactly the verified revision published, fast-forward only, with an
  expected-head lease;
- CI-configuration changes never published in M2.

ADR-0006 keeps source-control credentials out of the sandbox. Several git
mechanisms can execute code or leak data:
- hooks, `core.fsmonitor` and pagers;
- `diff`/`textconv`/`filter` drivers selected by `.gitattributes`;
- credential helpers;
- `url.<base>.insteadOf` rewrites;
- `ext::` transports;
- redirects;
- configuration from the user's home directory or the system.

Some porcelain also applies attributes from the tree itself: `checkout`
runs `smudge` filters, and `archive` applies `export-subst` and
`export-ignore`.

## Considered options

1. Clone into the workspace and let the agent's repository be the candidate.
   ADR-0010 rejects this.
2. Copy the workspace into a coordinator worktree, then `git add` and
   `commit`. A worktree reads `.gitattributes` from the copied content, so
   filters, `textconv` and `eol` rules chosen by the agent would apply.
3. **Plumbing only.** Read the workspace in Python and stream it into
   `git fast-import`. Write trees out with `ls-tree` plus `cat-file`. There is
   no worktree, so no attributes, filters or hooks are involved.

For the credential:
- **A.** Token in the remote URL: it lands in reflogs, error messages and
  the process list.
- **B.** A credential helper: it runs a program.
- **C.** An `http.<url>.extraHeader` passed through `GIT_CONFIG_COUNT`,
  `GIT_CONFIG_KEY_<n>` and `GIT_CONFIG_VALUE_<n>`.

## Decision

Option 3 with credential option C.

- **Hardened runner (`ilmar.adapters.git.runner`).** Each git process gets an
  environment built from scratch. No inherited `GIT_*` variable survives,
  nor any other variable. Its configuration comes only through `GIT_CONFIG_*`
  [R22], which overrides any configuration file:
  - no system config, `GIT_CONFIG_GLOBAL=/dev/null`, a private `HOME` and
    `XDG_CONFIG_HOME`;
  - `core.hooksPath=/dev/null` and no fsmonitor; pager, editor and
    `askPass` disabled; credential helpers reset; `core.attributesFile`
    and `core.excludesFile` set to `/dev/null`; `core.sshCommand=false`;
    `diff.external` empty;
  - `protocol.allow=never`, plus `https` only (`file` only in tests;
    `ext` and `fd` can never be enabled);
  - `http.followRedirects=false`; `transfer.fsckObjects` and
    `fetch.fsckObjects` on;
  - `safe.bareRepository=explicit`, with `--git-dir` always given;
  - gc, maintenance and signing off.

  The template's sample hooks are deleted from the coordinator's bare
  repository.
- **Freeze (`ilmar.adapters.git.trees`).** The workspace is walked through
  directory descriptors with `O_NOFOLLOW`.
  - The top-level `.git` is ignored.
  - A `.git` entry in any letter case below the top level is a nested
    repository and is rejected.
  - Special files and file names that are not UTF-8, or that git cannot
    store, are rejected.
  - Symlinks are stored as links. The executable bit is kept. Empty
    directories are dropped, as git does.
  - `fast-import` writes a commit with `deleteall` plus every entry on top
    of `base_revision`.
  - The author and committer are a fixed identity, timestamped with the
    base commit's committer time. The same content, base and message
    therefore always give the same revision.
  - The candidate stays reachable under `refs/ilmar/candidates/<id>`.
  - Changed paths come from `diff-tree --name-only --no-renames --no-ext-diff
    --no-textconv`.
- **Materialise.** Workspaces, resumes and verification checkouts are written
  from `ls-tree` and `cat-file --batch` into a fresh directory. Every
  component is opened relative to a directory descriptor with `O_NOFOLLOW`
  and `O_EXCL`, so nothing is ever written through a symlink. File modes are
  set on the descriptor, independently of the umask. Workspaces contain no
  `.git`. Trees with submodules, or unsafe or `.git` paths, are refused.
- **Credential.** For an `https` base URL, the token becomes
  `http.<base-url>/.extraHeader: Authorization: Basic …`, passed through the
  environment. That header is scoped by scheme, host, port and path prefix
  [R22]. The token is never in a URL, in argv, in a config file or in a
  workspace. Tests assert all four.
- **Publish.** The steps, in order:
  1. The revision must exist in the coordinator repository.
  2. If the remote head already equals the revision, publishing is a no-op.
  3. A different head from `expected_head` raises `LeaseConflictError`.
  4. The head is fetched, and `merge-base --is-ancestor` must hold, or
     `NonFastForwardError` is raised.
  5. The CI guard runs.
  6. An unforced `push --no-verify` follows, so the remote itself also
     refuses non-fast-forward updates.
  7. The remote head is read back. A head equal to the revision is success.
     A head that moved is a lease conflict. Anything else is a provider
     error.
- **CI guard (ADR-0010 §5, defence in depth).** A revision is refused with
  `CandidateRejectedError("ci-config-modified")` if any `ci_config_paths`
  entry (default `.github`) differs between the revision and its merge base
  with the current branch head, or with the default branch for a new branch.
  The publishing token should also lack workflow-write permission. The
  opt-in live test documents this.
- **Pull requests.** Ilmar's pull requests carry the marker
  `<!-- ilmar:change-request -->` and the task key.
  - An open pull request from the same head branch without the marker
    raises `OwnershipConflictError` and is never edited.
  - Ilmar's own pull request is updated in place.
  - Checks combine check runs and commit statuses, read across **all**
    pages until GitHub's `total_count` is reached (up to 50 pages of 100
    each). Any failure means FAILED, otherwise anything pending means
    PENDING, then PASSED, and NONE when nothing is reported.
  - If either list cannot be completed, an `ilmar: check results
    incomplete` entry is added and the summary is never PASSED: it is
    FAILED if a seen check failed, otherwise PENDING.

## Verification

- The **full** `SourceControlContract` runs against real git, with a bare
  repository over `file://`, and no case is skipped or overridden. That
  includes planted hooks, `.git/config` commands, `.gitattributes` filters,
  symlinks, nested repositories, special files and history rewriting.
- Unit tests cover:
  - a hostile `~/.gitconfig`, XDG config and `GIT_*` variables (hooks,
    fsmonitor, a credential helper, an alias and an `insteadOf` redirect)
    have no effect;
  - the environment is built from scratch;
  - the protocol allow-list;
  - the token appears only as a scoped header in the environment, never on
    disk;
  - the CI guard: modify, add and delete, on new and existing branches;
  - a branch that moves during publish is a lease conflict;
  - `.GIT` variants, non-UTF-8 names, quoting round trips and the umask;
  - branch and repository validation;
  - pull request ownership and updates;
  - check mapping, including a failure beyond the first page of check runs
    and of statuses, and a truncated result set failing closed.
- Mutation checks:

  | Mutation | Failing tests |
  | --- | --- |
  | Inherit the environment | 2 |
  | Force-push | 1 |
  | Drop the CI guard | 5 |
  | Drop the ancestry check (git's own refusal remains) | 1 |

- An opt-in live test (`pytest -m integration`) makes a full round trip on a
  disposable GitHub repository.

## Consequences

- Good:
  - Nothing an agent writes is ever interpreted by host git.
  - The same bytes are verified and published.
  - The credential never touches disk or argv.
  - The design does not depend on GitHub, so a GitLab adapter (#23) can
    reuse the git layer.
- Bad:
  - The freeze reads every file into memory once. That is fine for M2's
    repository sizes; revisit for large repositories.
  - The coordinator keeps a bare cache per repository, and a full fetch
    happens when a base revision is unknown.
  - Commits with more than 5,000 check runs or statuses are reported as
    incomplete (PENDING), never PASSED.
  - Changing CI configuration always needs a human in M2.

## Revisit when

- Repositories get large enough that a streaming freeze or partial fetches
  are needed.
- The approval policy (#20) allows Ilmar to publish CI changes behind
  explicit human approval.
- GitHub App installation tokens replace personal tokens. The header
  mechanism stays the same.
