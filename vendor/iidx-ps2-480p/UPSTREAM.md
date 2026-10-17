# iidx-ps2-480p (bundled copy)

- Source: https://github.com/blurtic2/iidx-ps2-480p
- Commit: `5b23bcba444ce7af799f614133681bd2168ae992` (2026-10-17)
- Files: `patch480p.py`, `mipsasm.py`, `LICENSE`
- License: MIT, Copyright (c) 2026 iidx-ps2-480p contributors
- Changes: none. The files are copied exactly as published.

`iidx480p_hdd.py` imports `patch480p.py` from this folder and uses its version table, verification and patch words directly.

To update: copy the two `.py` files from the upstream repository over these, change the commit above, and run `python tests/test_tools.py`.
