# Contributing

Thanks for helping make Friday better. Bug reports, device reports ("my
Hisense TV works / doesn't") and pull requests are all welcome.

## Before you open a pull request

1. Open an issue first for anything larger than a small fix, so we can agree
   on the approach before you spend time on it.
2. Run the tests: `.venv/bin/pytest`. They use fake devices and never touch
   a real TV, light or DoorDash account.
3. Keep personal details out of code, tests and screenshots: no real
   addresses, IPs outside the documented example ranges, API keys, names
   or voice recordings.
4. Match the style around you: plain names, short comments that say *why*,
   and a test for any behaviour change.

## Contributor license

Friday is offered free for noncommercial use and under paid licenses for
commercial use (see [COMMERCIAL.md](COMMERCIAL.md)). For that to keep
working, the maintainer needs the right to include your contribution in both.

By opening a pull request, you agree that:

- the contribution is your own work, or you have the right to submit it;
- you grant the maintainer (knightlie-code) a perpetual, worldwide,
  irrevocable, royalty-free license to use, modify, sublicense and
  distribute your contribution under the PolyForm Noncommercial License
  and under any commercial license the maintainer offers;
- you keep the copyright in your contribution and can use it however you
  like.

If you can't agree to this, please say so in the pull request before it is
merged.
