# Contributing

Thanks for helping. Bug reports, fixes and new sensors are all welcome. For anything bigger than a small fix, open an issue first, so we can agree on the approach before you write the code. Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).

## Two rules that do not bend

**The integration must never be able to trade or move money.** Every websocket request goes through `ReadOnlyApi` in `custom_components/trade_republic/api.py`, which refuses any subscription that is not in `ALLOWED_SUBSCRIPTIONS`, and refuses payouts outright. A new request type joins that list only if it reads and nothing else, and the pull request has to say why. `test_refuses_requests_that_are_not_allowed` in `tests/test_api.py` lists the pytr calls that must stay refused: when pytr gains a call that writes, add it there.

**No real account data, anywhere.** Never put a phone number, PIN, session cookies (`*.cookies`), real amounts, positions or ISINs of your own portfolio in an issue, a test, a fixture or a screenshot. Diagnostics contain none of these and can be attached as they are. Debug logs do contain your positions: remove them before posting. Screenshots in `docs/images` use made-up data.

## Reporting a security problem

If you find a way for the integration to send a request that is not read-only, or to leak the PIN or the session, do not open a public issue: see [SECURITY.md](SECURITY.md).

## Development

You need Python 3.14, the version the tests run on.

```
python -m venv .venv
.venv/bin/pip install -r requirements_test.txt      # .venv\Scripts\pip on Windows
pytest
ruff check . && ruff format --check .
```

The tests never reach Trade Republic: they talk to a fake websocket (`FakeWebSocket` in `tests/test_api.py`), and the Home Assistant test plugin blocks the network. The same three commands run on every push and pull request.

## What a pull request needs

- One change per pull request, with tests for the behaviour it adds or fixes.
- Texts shown in Home Assistant live in `strings.json` and `translations/en.json` (identical), `de.json` and `it.json`. Add every new key to all four; if you cannot translate it, leave the English text and say so in the pull request.
- Update `README.md` when entities, attributes or options change.
- AI coding tools are fine, as they were for this project: read and test what you submit as if you had written it, and say in the pull request that you used one.
- Commit messages say what changes for the person using the integration, for example *Keep isin off the next dividend sensor, which is not a security*.

## Dependencies

Requirements are pinned to exact versions in `manifest.json`, and repeated with the same versions in `requirements_test.txt` so the tests use what Home Assistant installs. Change them together.

[Renovate](https://docs.renovatebot.com) opens the updates on Monday mornings, once a release is at least 7 days old (`renovate.json`). Updates of the test tools and of the GitHub Actions merge themselves when the tests pass. Updates of `manifest.json` are merged by hand after reading the changelog, because they change the code that runs in Home Assistant, and they reach users only with a release.

## Releases

1. Bump `version` in `custom_components/trade_republic/manifest.json`.
2. Publish a GitHub release tagged `v` plus that version, for example `v0.1.2`.
3. The **Release** workflow checks that the tag matches the manifest and attaches `trade_republic.zip`, which is what HACS installs.
