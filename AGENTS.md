# Notes for AI agents

- **Installing Friday for someone?** Follow
  [skills/install-friday/SKILL.md](skills/install-friday/SKILL.md).
- **Working on the code:** Python 3.11+, FastAPI app in `src/house_agent/`,
  static web app in `src/house_agent/static/` (plain JS, no build step; bump
  the `?v=` on a changed file in `index.html`). Run tests with
  `.venv/bin/pytest`. They use fakes and never touch real devices.
- Never commit `.env`, `config.toml` or `data/`. Keep personal details (real
  addresses, IPs, names, keys) out of code and tests.
- Friday's code is under the PolyForm Noncommercial license. See `LICENSE`
  and `CONTRIBUTING.md`.
