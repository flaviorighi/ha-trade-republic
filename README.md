# Trade Republic for Home Assistant

Brings the portfolio of a [Trade Republic](https://traderepublic.com) account into Home Assistant: total value, cash, invested capital, unrealized gain, and one sensor for every security you hold.

> **Unofficial.** This project is not affiliated with Trade Republic Bank GmbH. It uses the same private API as Trade Republic's web app, through [pytr](https://github.com/pytr-org/pytr). Trade Republic can change or block that API at any time, and the integration then stops working until it is updated.

## Read-only by design

- **It cannot trade or move money.** pytr can place orders, change savings plans and request payouts. This integration wraps pytr in a client that refuses every request outside a short list of read-only ones (portfolio, cash, instrument details, prices), and the tests check that every order, savings plan, alarm, watchlist and payout call is refused. Outside that connection it only reads the account settings and the portfolio chart.
- **Your PIN is never stored.** It is sent once to Trade Republic to log in, and the login is confirmed in the Trade Republic app. Afterwards the integration only uses the session cookies. When the session expires, Home Assistant asks for the PIN again.
- **The session is stored.** The cookies live in `.storage/trade_republic/` inside your Home Assistant configuration, so they are also in your backups. Like the session of a browser logged into Trade Republic, they give access to the account until they expire: protect your configuration and encrypt your backups.
- **Dependencies are pinned** to exact versions, so the code that runs changes only with a release of this integration.
- **Diagnostics contain no amounts, securities or account details**, so they can be attached to a public issue.

## Installation

### HACS

1. In HACS, open the menu → **Custom repositories**, add `https://github.com/flaviorighi/ha-trade-republic` with type **Integration**.
2. Install **Trade Republic** and restart Home Assistant.

### Manual

Copy `custom_components/trade_republic` into the `custom_components` folder of your configuration and restart Home Assistant.

## Setup

1. **Settings → Devices & services → Add integration → Trade Republic.**
2. Enter your phone number in international format (for example `+49 151 23456789`) and your PIN.
3. Confirm the login in the Trade Republic app on your phone. The setup continues on its own; the request expires after about 2 minutes. Accounts that use an authenticator app are asked for its code instead.

Logging in from Home Assistant does not log you out of the app on your phone.

## Entities

All amounts are in the account currency.

| Entity | Meaning |
| --- | --- |
| Total value | Cash plus the market value of all securities |
| Securities value | Market value of all securities |
| Cash | Available cash |
| Invested | What the securities cost, from their average purchase price |
| Unrealized gain | Securities value minus invested, for the securities with a purchase price |
| Unrealized return | Unrealized gain as a percentage of invested |
| *One per security* | Market value, with `isin`, `quantity`, `average_price`, `price`, `invested`, `gain` and `gain_percent` as attributes |

A sensor appears when you buy a new security. When you sell all of it, its sensor becomes unavailable and is removed at the next restart.

Prices are the last trade on the security's first exchange, usually Lang & Schwarz. Bonds are quoted in percent and converted. If a price does not arrive, the last known one is kept.

## History

Home Assistant keeps statistics only from the moment the integration is set up. On the first start the integration therefore reads, once, the portfolio chart shown in the Trade Republic app and imports it into the long-term statistics of the sensors it describes: securities value (or total value), invested and unrealized gain, up to the first statistic Home Assistant recorded itself. Statistic graphs then show the portfolio before the integration existed.

A series is imported only if its latest point agrees with today's value of the sensor within 1% of the securities value, so a change in what Trade Republic sends cannot fill a sensor with the wrong numbers. There is no history for cash or for single securities.

## Options

**Update interval**: how often the portfolio is read, 30 minutes by default (15 minutes to 24 hours). Every update opens one connection and sends one request per security.

## When the session expires

Trade Republic ends web sessions after a while. Home Assistant then shows a repair to log in again: enter your PIN and confirm in the app. Until then the sensors are unavailable.

## Troubleshooting

Enable debug logging to see what is exchanged with Trade Republic (it includes your positions, but never the PIN or the cookies):

```yaml
logger:
  logs:
    custom_components.trade_republic: debug
```

## Roadmap

- Dividends and interest received
- Savings plans

## Credits

Built on [pytr](https://github.com/pytr-org/pytr) (MIT), which keeps up with Trade Republic's login and protocol.

## License

MIT
