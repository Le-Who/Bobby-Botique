# Dependency Frontier Report

- Base SHA: `abc123`
- Cutoff: `2026-08-22T10:30:00Z`
- uv: `0.12.6`
- Python/platform: `3.14` / `x86_64-manylinux_2_28`
- Resolved platforms: `x86_64-manylinux_2_28, x86_64-pc-windows-msvc`
- Input hash: `902579ef3d874dd517a3b14c0b1ae30ad42fa975757cc88a38fdb7e57a78af78`

| Package | Scope | Constraint | Locked | Policy | Frontier | Classification |
|---|---|---|---:|---:|---:|---|
| demo | production | `demo<2,>=1` | 1.5 | 1.9 | 2.1 | blocked-by-policy |
| pytest | development | `pytest<10,>=9` | 9.0 | 9.1 | 9.1 | update-within-policy |
