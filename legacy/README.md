# legacy/ — superseded Atlas MES build (frozen)

The previous-generation MES (SQLite, read-only vs JB2, quantity-based WIP) and its
governing docs, superseded by `documentation/` (July 2026 design set) per
`docs/CHANGE_REQUESTS.md` CR-001.

- Reference only. **Nothing in the new system may import from `legacy/`** (CI-enforced from P1-12).
- Still runnable: `cd legacy && python3 test_smoke.py`.
- Useful reference: JB2 wire-format constants in `app/services/jb2_adapter.py`,
  base-template pattern in `app/templates/engineer/base.html`, test philosophy in `test_smoke.py`.
