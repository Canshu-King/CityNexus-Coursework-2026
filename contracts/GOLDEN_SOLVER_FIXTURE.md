# Golden Solver Fixture - Small Semantic Oracle

This is a deterministic micro-fixture for proving the first CP-SAT model before wiring it into the database.

Journey starts at 09:00, so integer minute offsets are:

```text
13:25 disruption now = 265
15:30 River Cruise C4 = 390
19:00 Journey end     = 600
```

Current location:

```text
place-tech-art
```

Protected next C4:

```text
place-river-cruise
start = 390
duration = 60
```

Replacement options:

```text
Design Library
  duration 60
  cost 2000 minor CNY
  preference 900/1000

City Park
  duration 60
  cost 0
  preference 300/1000
```

Frozen mobility:

```text
Tech Art -> Design Library = 20
Design Library -> River Cruise = 20
Tech Art -> City Park = 20
City Park -> River Cruise = 20
Tech Art -> River Cruise = 20
```

Safety buffer:

```text
15 minutes
```

Expected semantic candidates for `candidate_count=3`:

```text
1. Design Library
   earliest start = 13:45 (285)
   end = 14:45 (345)
   + outbound 20
   + safety 15
   = 15:20, safely before 15:30 C4

2. City Park
   same feasible timing, lower verified preference

3. DROP
   direct current -> C4 remains feasible
```

The exact CP-SAT branch must be repeatable and match the independently readable reference enumerator for this fixture.

This fixture is intentionally tiny. It proves solver semantics; it is not the final Journey optimization model.


