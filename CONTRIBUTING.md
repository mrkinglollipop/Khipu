# Contributing to Khipu

Thanks for your interest. Please read the licensing section before opening a
pull request — submitting one is how you accept those terms.

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

Contributions beyond a trivial fix require a signed
[Contributor Licence Agreement](CLA.md).

This one matters here. AGPL-3.0 alone would leave every contributor holding veto
power over any future licence change, because the maintainer cannot relicense
code he does not own. The CLA grants the right to relicense — which is what
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

**Run the checks.** All three must pass:

```bash
PYTHONPATH="packages/cli:.python_libs" python3.11 -m unittest discover -s packages/cli/tests -q
```

```bash
cd apps/desktop/src-tauri && cargo test
```

```bash
ruff check packages/cli
```

**Never commit** credentials, connection strings, certificates, private keys,
`.env` files, or anything under a personal path. The test suite uses obvious
fixtures (`postgresql://u:pw@h/db`) — follow that pattern rather than pasting a
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
