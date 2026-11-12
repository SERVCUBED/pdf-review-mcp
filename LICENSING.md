# Licensing

Copyright 2026 Ben Blain.

This repository is a mixed work: **software** under the GNU Affero General Public License, and
**documentation** under Creative Commons Attribution-ShareAlike. Machine-readable per-file
information is in [`REUSE.toml`](REUSE.toml), which follows the
[REUSE Specification 3.3](https://reuse.software/spec-3.3/). This page is a plain-language
summary. **The license texts themselves are the only legally binding terms.**

## Scope

| What | License | SPDX |
|---|---|---|
| Software: Python source (`src/`), tests (`tests/`), scripts (`scripts/`), example configs (`examples/`), build and project configuration (`pyproject.toml`, `REUSE.toml`), CI (`.github/`), and repository configuration files (`.gitignore`, `.gitattributes`, `.editorconfig`) | GNU Affero General Public License v3.0 **or (at your option) any later version** | `AGPL-3.0-or-later` |
| Documentation: `README.md`, `AGENTS.md`, `SECURITY.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `LICENSING.md`, `THIRD_PARTY_NOTICES.md` and `docs/` | Creative Commons Attribution-ShareAlike 4.0 International | `CC-BY-SA-4.0` |
| **Code snippets** in that documentation (commands, configuration and code inside fenced code blocks) | CC-BY-SA-4.0, **and additionally** AGPL-3.0-or-later, at your choice | `CC-BY-SA-4.0 OR AGPL-3.0-or-later` |
| The license texts in `LICENSE` and `LICENSES/` | Published by the Free Software Foundation and Creative Commons, respectively; reproduced verbatim | n/a |

* The extra grant for snippets exists so you can copy installation commands, configuration and
  code examples from the documentation into software under the same terms as the rest of the
  code.
* **License-text provenance:** The AGPL text carries the Free Software Foundation copyright notice
  and verbatim-copy condition. Creative Commons has waived copyright and related rights in the CC
  public-license text to the extent possible; Creative Commons is not a party to this project's
  license grant. The project claims no copyright in either official license text.

## License texts and provenance

| File | Content | Official source | Retrieved | SHA-256 |
|---|---|---|---|---|
| `LICENSE` | GNU AGPL v3, verbatim | <https://www.gnu.org/licenses/agpl-3.0.txt> (fetched via `http://` after the HTTPS connection was reset) | 2026-11-12 | `0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0` |
| `LICENSES/AGPL-3.0-or-later.txt` | byte-identical copy of `LICENSE`, as the REUSE Specification requires | same as above | 2026-11-12 | `0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0` |
| `LICENSES/CC-BY-SA-4.0.txt` | CC BY-SA 4.0 legal code, verbatim (plain-text edition) | <https://creativecommons.org/licenses/by-sa/4.0/legalcode.txt> (server `Last-Modified: 11 Sep 2023`) | 2026-11-12 | `28a9529c7d0bb4dc51f4bf5c116a3d16ef247a052f7591466768ddf563fd1cf5` |

Both texts were cross-checked word by word against the independent
[SPDX license-list-data](https://github.com/spdx/license-list-data) copies.
- **AGPL:** identical apart from three `http`/`https` URL schemes.
- **CC:** identical apart from the non-binding preface links, the public-domain notice for the
  license text, and subsection lettering, which SPDX normalises.

`LICENSE` and `LICENSES/AGPL-3.0-or-later.txt` are the **same file**. The duplicate exists only
for tooling (GitHub detects `LICENSE`; REUSE requires `LICENSES/<SPDX-ID>.txt`). It does not
mean two different licenses.

## What this means in practice (summary, not legal advice)

### Software (AGPL-3.0-or-later)

* **Use, study and modify:** you may run, study and modify it freely, including commercially.
  There is no non-commercial restriction.
* **Conveying copies** (distributing source or binaries, modified or not):
  - keep the copyright and license notices;
  - provide the complete corresponding source under the AGPL;
  - **mark modified versions as changed**, with a relevant date (AGPL section 5).
* **Network use (section 13):** if you **modify** the program and let users interact with it
  **remotely over a network**, you must offer those users the corresponding source of your
  modified version. Running an unmodified copy, or using it only privately, does not trigger
  this.
* **"Or any later version":** each recipient may follow version 3 or any later version of the
  AGPL published by the FSF. Combining with GNU GPLv3-licensed code is explicitly permitted by
  section 13. "Or later" does **not** relicense the work under unrelated licenses.

### Documentation (CC-BY-SA-4.0)

* **Share and adapt** it for any purpose, including commercially.
* **Attribution:** credit "Ben Blain", link to the license, and **indicate whether you changed
  the material**.
* **ShareAlike:** adaptations you share must be under CC BY-SA 4.0, a later version of it, or a
  [BY-SA Compatible License](https://creativecommons.org/compatiblelicenses) as listed by
  Creative Commons. That list includes the Free Art License 1.3 and, one-way, GPLv3.
* You may not apply legal terms or technological measures that restrict others from doing what
  the license permits.

### Suggested attribution

> "pdf-review-mcp" documentation by Ben Blain, licensed under CC BY-SA 4.0
> (https://creativecommons.org/licenses/by-sa/4.0/). Changes: <describe your changes>.

## Third-party components

The dependencies (FastMCP, PyMuPDF/MuPDF and their own dependencies) are **not** part of this
copyright or license grant and are not distributed in this repository. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
