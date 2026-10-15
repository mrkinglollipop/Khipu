# Contributing to Khipu

Thanks for your interest. Please read the licensing section before opening a
pull request. Submitting one is how you accept those terms.

---

## Licensing of contributions

Khipu is released under the [GNU Affero General Public License v3.0](LICENSE).

**Every contribution you submit is licensed under AGPL-3.0 as well.** Submitting
a pull request is how you accept that.

What AGPL-3.0 means for anyone using Khipu:

- They may use, modify and redistribute it freely.
- If they distribute a modified version, they must release their source under
  AGPL-3.0 too.
- **If they run a modified version as a network service, they must offer its
  source to every user of that service** (Section 13). Hosting it instead of
  shipping it does not avoid the obligation.

You keep the copyright on what you wrote. The licence does not take it away.

You may not submit code you do not have the right to license this way. If your
employer holds rights to work you do, get their permission first.

### Contributor Licence Agreement

Contributions require a signed
[Contributor Licence Agreement](CLA.md). To sign, add your name to the table in
`CLA.md` in your first pull request, or in one before it.

This one matters here. AGPL-3.0 alone would leave every contributor holding veto
power over any future licence change, because the maintainer cannot relicense
code he does not own. The CLA grants the right to relicense, which is what
makes it possible to sell a commercial licence to companies that cannot accept
AGPL terms. Sign it once and it covers all your future contributions.

### Sign your commits (DCO)

Every commit must carry a `Signed-off-by` line certifying the
[Developer Certificate of Origin](https://developercertificate.org/):

```bash
git commit -s -m "your message"
```

That appends:

```
Signed-off-by: Your Name <your.email@example.com>
```

By signing off you certify that you wrote the contribution, or otherwise have
the right to submit it under the project's licence.

---

## Before you open a pull request

Khipu talks to a live PostgreSQL instance and to several agent harnesses, so
the bar for a change is a bit higher than the code size suggests.

**Set up once.** The CLI needs Python 3.11 and the packages in
`packages/cli/requirements.txt`. The tests and linter need two more, which are
not in that file:

```bash
python3.11 -m pip install --target .python_libs -r packages/cli/requirements.txt
python3.11 -m pip install pytest ruff
```

(`.python_libs` is gitignored. If you install the requirements somewhere else,
point `PYTHONPATH` at that place in the next step.)

**Run the checks.** Each block starts from the repo root. All of them must pass
for the parts you changed:

```bash
cd packages/cli
T=$(mktemp -d)
HOME="$T" KHIPU_KEYCHAIN=0 PYTHONPATH="$PWD:$PWD/../../.python_libs" python3.11 -m pytest -q
```

```bash
cd apps/desktop/src-tauri && cargo test
```

```bash
cd apps/desktop && npm install && npm run check:setup
```

```bash
ruff check packages/cli
```

(The `ruff` block is the only one that does not change directory first.)

The pytest command is the test oracle. Run it from `packages/cli`, under a
temporary `HOME`, with `KHIPU_KEYCHAIN=0`. Read its exit code. Run the `cargo`
and `npm` checks when you touch the desktop app. `npm run build` also runs the
TypeScript check (`tsc`).

The suite is **hermetic by default**: before anything imports `khipu`,
`packages/cli/tests/conftest.py` points `HOME` at a throwaway temporary
directory and clears every Khipu/Alzy credential and identity variable from
the environment, so a normal run never touches your real hooks, Keychain, or
configured hub. Even the read-only commands (`status`, `verify`, `doctor`) used
to re-point live hook launchers just by being asked for their path. The
temporary `HOME` and `KHIPU_KEYCHAIN=0` in the command above are a second
layer of the same protection. Real PostgreSQL connections are refused.

`KHIPU_LIVE_TESTS=1` turns that isolation off entirely: the real `HOME`, the
real Keychain and your configured hub. Some tests then write and delete their
own probe rows on the hub and call the real embedding API. A few also need
`KHIPU_SCRATCH_DSN` and `KHIPU_SCRATCH_NONCE`. Do this deliberately, never as
your default way of running the suite.

To try the CLI by hand without touching your own setup, use the same trick:

```bash
cd packages/cli
HOME="$(mktemp -d)" KHIPU_KEYCHAIN=0 PYTHONPATH="$PWD:$PWD/../../.python_libs" python3.11 -m khipu --help
```

**Never commit** credentials, connection strings, certificates, private keys,
`.env` files, or anything under a personal path. The test suite uses obvious
fixtures (`postgresql://u:pw@h/db`). Follow that pattern rather than pasting a
real value and editing it down.

**Treat the database as read-only** unless your change is explicitly about
schema migration, and say so plainly in the pull request description.

**Describe what you verified.** State which of the checks above you ran and what
they returned. "Should work" is not a test result.

---

## Reporting a security issue

Do not open a public issue for a security problem. Report it privately through
[GitHub's security advisory form](https://github.com/mrkinglollipop/Khipu/security/advisories/new)
or by email to [support@kinglollipop.com](mailto:support@kinglollipop.com).

## Getting help

General questions: [support@kinglollipop.com](mailto:support@kinglollipop.com).
