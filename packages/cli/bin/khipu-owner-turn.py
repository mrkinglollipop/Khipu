"""khipu-owner-turn: record one prompt-submit turn for owner direction
(``khipu.owner_turns``). Run by ``khipu-prompt-recall`` as ``python3 -S -E
-B`` before it answers, with the hook payload on stdin; prints nothing.
Standard library plus the three small stdlib-only khipu
modules it needs, loaded from beside this script: ``..`` in the repo,
``../packages/cli`` in the app bundle's ``bin``. Any failure prints nothing
and exits 0, so a prompt is never failed or held by the recorder.
"""
import os
import sys


def main():
    here = os.path.dirname(os.path.realpath(__file__))
    for rel in (("..",), ("..", "packages", "cli")):
        root = os.path.normpath(os.path.join(here, *rel))
        if os.path.isfile(os.path.join(root, "khipu", "owner_turns.py")):
            sys.path.insert(0, root)
            break
    else:
        return 0
    try:
        from khipu import owner_turns

        return owner_turns.main(sys.argv)
    except BaseException:  # noqa: BLE001 - the recorder must never fail a prompt
        return 0


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # noqa: BLE001
        pass
    sys.exit(0)
