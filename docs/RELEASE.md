# Cutting a release

A release is four bytes-wide facts that have to agree: the version in the
source, the name of the wheel, the tag, and the checksum `install.sh`
verifies against. Everything here exists to keep them from drifting apart —
which they once did, when `pyproject.toml` said 1.4.0 while
`src/stella/__init__.py` still said 0.1.0.

## One number, read from one place

`src/stella/__init__.py` holds `__version__`, and `pyproject.toml` declares
`dynamic = ["version"]` with `[tool.hatch.version]` pointing at that file. So
editing one line moves the tag's expected wheel name, `stella --version`, the
version `stella doctor` reports, and the version the release workflow checks
the built wheel against. `tests/test_package.py` asserts the package metadata
and the module agree, so a stale number fails CI rather than shipping.

## The steps

1. Land the work on `master`, with a `CHANGELOG.md` entry for each meaningful
   change.
2. Promote the changelog: move the `Unreleased` section under a
   `## X.Y.Z (date)` heading.
3. Set `__version__ = "X.Y.Z"` in `src/stella/__init__.py` and commit.
4. Tag it and push both: `git tag vX.Y.Z && git push && git push origin vX.Y.Z`.
5. Watch the `Release` workflow. It is the only thing that publishes; there is
   no manual upload step and nothing to remember afterwards.

A version bump is its own commit, so `git log` shows exactly which bytes went
into which release. `v1.0.0`–`v1.4.0` exist and stay where they are.

## What the workflow proves before anything is visible

`.github/workflows/release.yml` triggers on a `v*` tag push and, in order:

- **Reads the version from the tag safely.** `GITHUB_REF_NAME` is read as an
  environment variable and narrowed to digits and dots before any step that
  expands it — a tag is only allowed to be a version, never shell text.
- **Builds** the wheel and sdist with `uv build`.
- **Installs the wheel into a clean environment** and checks three things
  about the artifact people actually get: the imported module reports the
  tagged version, `stella --version` agrees, and `stella doctor --json` runs
  on a machine with no Stella state at all and reports that same version.
- **Writes the checksums** `install.sh` looks for: a per-file
  `stella-<version>-py3-none-any.whl.sha256` holding `"<hash>  <filename>"`,
  plus a combined `sha256.txt`.
- **Only then publishes**, with `gh release create --verify-tag` — which
  refuses to invent or move a tag. If the release already exists, the run
  replaces its assets (`--clobber`) and touches no tag and no history.

Order matters and `tests/test_release_workflow.py` pins it: a workflow that
published before installing would publish unverified bytes. The same test file
pins the other two contracts that can break silently — the asset names
`install.sh` downloads, and the fact that CI installs the wheel it built
rather than the one on PyPI.

## Never move a tag

Once a release has been installed by checksum, it has to stay the exact bytes
it was published as. Retracting a bad release means publishing a *newer*
version, not repointing a tag: `install.sh` caches nothing, but someone's
`~/.local` does, and a tag that changed under an installed wheel is how a
verified download stops meaning anything. If a tag is wrong before anyone
installed it, delete the draft release rather than the tag.

## The repository has to be public

`curl -fsSL https://raw.githubusercontent.com/<owner>/<repo>/HEAD/install.sh | sh`
is an unauthenticated request: while the repository is private, GitHub answers
404 and the front door does not exist. Making it public also makes the commit
history public, including the author name and email on every commit — check
that before flipping the switch, because it is effectively irreversible once
anything indexes it.

## What a wheel cannot carry

A release is Python and nothing else. The recorder, the transcriber, the
speech worker, the VAD and wake-word `.onnx` files, a pulled model, a
browser, Tkinter and bubblewrap all live outside the package, so an install
can be complete and Stella still silent. That gap is why `stella doctor`
exists: it probes each of those, in the same order and with the same resolvers
the running app uses, prints what is missing and how to fix it, and creates
nothing while doing so. `install.sh` ends by pointing at it rather than
pretending the job is finished.

## Development tree versus installed release

`~/Projects/Stella` (or any checkout) is for development; day-to-day use is
the installed release. They must not share state, because a work-in-progress
branch reading your real memories, persona and API key can write back to them.

`bin/stella-dev` is the whole mechanism: it points `XDG_DATA_HOME` and
`XDG_CONFIG_HOME` at the checkout's own `.dev/` directory, moves the voice
socket there with `STELLA_VOICE_SOCKET`, and runs the tree with `uv run`.

```bash
bin/stella-dev doctor      # this tree, this tree's state
bin/stella-dev ui          # the window from the working branch
bin/stella-dev voice --toggle
```

It deliberately does **not** redirect `XDG_RUNTIME_DIR`: PipeWire and Wayland
find their sockets there, and a wrapper that moved it would leave Stella
unable to hear or see the desktop. `.dev/` is gitignored and holds only state,
never code — deleting it is like clearing a profile, not like losing work.

Pointing the tree at real state is possible (`uv run stella …`) and is a
deliberate act, not the default. `tests/test_dev_wrapper.py` runs the wrapper
end-to-end against a substituted `HOME` and asserts every state path it
reports lands under `.dev/`, and that the run created nothing.
