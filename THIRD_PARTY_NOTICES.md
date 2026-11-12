# Third-party notices

This repository contains only its own source, tests, scripts and documentation. **It does not
bundle or redistribute third-party code.** The packages below are installed separately, by pip
or uv, at install time. Their copyright belongs to their respective authors, under their own
licenses; nothing in this project's [LICENSING.md](LICENSING.md) applies to them.

| Dependency | Role | License (as published by the project) |
|---|---|---|
| [PyMuPDF](https://pymupdf.readthedocs.io) (with MuPDF) | PDF parsing, annotations and rendering | **Dual-licensed: GNU AGPL-3.0, or a commercial license from Artifex Software, Inc.** |
| [FastMCP](https://gofastmcp.com) | MCP server and client framework | see the package's own metadata |
| [MCP Python SDK](https://modelcontextprotocol.io), Pydantic and further transitive dependencies | installed by FastMCP | see each package's own metadata |

* **PyMuPDF:** this project is AGPL-3.0-or-later, which is consistent with using PyMuPDF under
  its AGPL option. If you need to distribute this software under terms the AGPL does not allow,
  you would need a commercial PyMuPDF license from Artifex as well as permission for this
  project's code.
* **No clearance guarantee:** the full dependency tree has **not** been audited for license
  compatibility. Before redistributing, especially as a bundle such as a container image or a
  frozen binary, check the licenses of the exact versions you ship, for example with
  `pip-licenses` or by inspecting each package's `*.dist-info/` license files.
