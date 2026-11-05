<!--
Pull requests that do not follow this template fail the "PR template" check and are closed.
Open an issue first: every pull request must close an accepted issue.
-->

## Issue

Closes #

## Change

<!-- One change. What it does and why, in a few sentences. -->

## Tests

<!-- Paste the last lines of each command you ran (the "Ran N tests ... OK" lines). -->

```text

```

## Checklist

- [ ] This pull request closes exactly one issue, linked above.
- [ ] It makes one change; unrelated edits, reformatting and renames are left out.
- [ ] The full test suites pass (README → Development and tests), and new behavior has a test.
- [ ] `node tools/web-checks/check-web-ui.js` passes if `web.html` or `i18n/*.json` changed, and both languages are updated.
- [ ] `docs/host-api.md`, `FRONTEND_API.md` and `frontend-contract.json` are updated if a route changed.
- [ ] It contains no credentials, tokens, account or device identifiers, personal data, note text or audio.
- [ ] It contains no code, strings or assets copied from the vendor's apps, and no tool for extracting credentials.
- [ ] It does not bypass any vendor limit, quota, payment or access check.
- [ ] I agree to license this contribution under GPL-3.0.
