#!/usr/bin/env python3
"""ORD-0394 Z3 -- take one place row out of **this run's own** private sqlite, so the unjoinable
branch of the coordinate projection is measured against a real store instead of a mocked payload.

`places.latitude` is NOT NULL in both the model and the live schema, so the literal counter-example
"clear a place's coordinate" is not a state the product can hold. The representable one is the place
row being absent while the activity still names it -- which is what this deletes.

The guard is the point of the file: a script that can delete a row must not be able to reach a shared
database by accident or by argument.
"""
import os
import re
import sqlite3
import sys

SHARED_MARKERS = ("_preview.db", "citynexus.db", "data/mock")


def main(argv):
    if len(argv) != 3:
        print("USAGE: ord0394_store_edit.py <sqlite-path> <place-id>", file=sys.stderr)
        return 2
    path, place_id = argv[1], argv[2]
    norm = path.replace("\\", "/")
    if not norm.startswith("sqlite:///") and not norm.endswith(".db"):
        return fail("refusing: %s is not a .db path" % norm)
    plain = norm[len("sqlite:///"):] if norm.startswith("sqlite:///") else norm
    for marker in SHARED_MARKERS:
        if marker in plain.lower():
            return fail("refusing: %s looks like a shared database (%s)" % (plain, marker))
    if not re.search(r"_cmdr\w+\.db$", plain):
        # the runner names its per-attempt database `_cmdr<runid>.db`; anything else is not this run's
        return fail("refusing: %s is not a per-attempt e2e database (_cmdr<run>.db)" % plain)
    if not os.path.isfile(plain):
        return fail("refusing: %s does not exist" % plain)

    con = sqlite3.connect(plain)
    before = con.execute("select count(*) from places").fetchone()[0]
    held = con.execute("select name, latitude, longitude from places where id = ?", (place_id,)).fetchone()
    if held is None:
        con.close()
        return fail("place %s is not in this store; the cell picked the wrong row" % place_id)
    users = con.execute(
        "select count(*) from activities where place_id = ?", (place_id,)
    ).fetchone()[0]
    # This connection does not enable foreign-key enforcement (sqlite default), so the delete leaves
    # `activities.place_id` dangling -- that dangling reference *is* the counter-example state.
    con.execute("delete from places where id = ?", (place_id,))
    con.commit()
    after = con.execute("select count(*) from places").fetchone()[0]
    con.close()
    print("E4F_STORE_EDIT db=%s place=%s name=%r held=%s,%s places %d->%d activity_rows_naming_it=%d"
          % (os.path.basename(plain), place_id, held[0], held[1], held[2], before, after, users))
    return 0


def fail(msg):
    print("E4F_STORE_EDIT_REFUSED " + msg, file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
