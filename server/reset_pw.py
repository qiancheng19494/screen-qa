#!/usr/bin/env python3
"""Reset a local account password.

Usage:  python reset_pw.py <user> [<new-password>]
Server: sudo python3 /srv/sqa/server/reset_pw.py azureuser
"""
import secrets
import sys

import store


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    name = sys.argv[1].strip()
    pw = sys.argv[2] if len(sys.argv) > 2 else ""
    if not pw:
        pw = secrets.token_urlsafe(9)
        generated = True
    else:
        generated = False
    store.init()
    con = store.connect()
    try:
        row = con.execute("select id from users where name = ?", (name,)).fetchone()
        if not row:
            print(f"no such user: {name}")
            return 1
        salt = secrets.token_hex(16)
        con.execute("update users set pw = ?, salt = ? where id = ?",
                    (store._hash(pw, salt), salt, int(row["id"])))
        con.execute("delete from tokens where user_id = ?", (int(row["id"]),))
        con.commit()
    finally:
        con.close()
    print(f"ok: password for {name} has been reset" + (" (generated)" if generated else ""))
    print(f"new password: {pw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
