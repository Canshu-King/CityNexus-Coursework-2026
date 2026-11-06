# OR-Tools Version Note - 2026-09-10

For the first Windows Solver Runtime integration, prefer a reproducible optional pin:

```toml
solver = ["ortools==9.15.6755"]
```

Why this pin is reasonable for the current mutation batch:

- current PyPI metadata exposes OR-Tools 9.15.6755;
- wheels are available for Windows x86-64 and CPython 3.12 (and current metadata also lists newer supported Python versions);
- current official CP-SAT Python documentation uses the snake_case API style used by the reference adapter (`new_int_var`, `model.add`, `solver.solve`).

After the real Windows branch is green, the project may deliberately widen the compatible range in a later dependency-maintenance change. Do not mix dependency-range experimentation into the first runtime integration.

The optional solver extra must not become necessary for the default offline demo.


