"""Desktop argv contracts, without running handlers or accessing live state."""
import argparse
import json
from unittest.mock import patch

from khipu import cli


def test_every_desktop_double_dash_argv_shape():
    for words in ("plain words", "--from-app", "-x words", "--"):
        _check_desktop_argv(words)


def _check_desktop_argv(words):
    # Mirror App.tsx and src-tauri/src/lib.rs, including typed ids/paths:
    # semantic validation belongs to handlers; parsing must preserve each token.
    cases = []

    def case(prefix, tail, **expected):
        cases.append(([*prefix, "--", *tail], expected))

    for verb in ("pause", "lift", "clear"):
        case(["route", verb, "--from-app"], [words], account=words, from_app=True)
    case(["route", "pause", "--from-app", "--until=2h"], [words], account=words, until="2h")
    case(["route", "clear", "--from-app", "--report=e1"], [words], account=words, report="e1")
    case(["route", "check"], [words], account=words)
    case(["route", "note", "--from-app"], ["set", words], action="set", text=words)
    case(["route", "note", "--from-app"], ["end"], action="end", text=None)
    for action in ("add", "edit", "retire", "restore", "confirm", "undo", "dismiss"):
        params = ([] if action == "add" else ["1"]) + ([words] if action in ("add", "edit") else [])
        case(["route", "rule", "--from-app"], [action, *params], action=action, params=params)
        # Terminal/test callers also put the verb before the flags.
        case(["route", "rule", action, "--from-app"], params, action=action, params=params)
    case(["route", "note", "set", "--from-app"], [words], text=words)
    for scope in ("--all", "--machine", "--account=Codex"):
        for until in ([], ["--until=2h"]):
            case(["route", "hold", "--from-app", "--reason=other", scope, *until],
                 [words], words=[words], reason="other", from_app=True)
    case(["route", "hold", "--from-app"], ["end", words], words=["end", words])
    entries = json.dumps([{"account": "Codex", "role": words}])
    for action in ("set", "confirm", "undo", "clear"):
        flags = [f"--entries={entries}", "--until=2h", '--holds={"h1":"keep"}'] if action == "set" else []
        case(["route", "priority", "--from-app", "--expect=p1", *flags], [action], action=action)
    # Each routing write may carry the app's retry identity before the separator.
    for argv, expected in list(cases):
        at = argv.index("--")
        if "--from-app" in argv[:at]:
            cases.append(([*argv[:at], "--op=synthetic-click-1", *argv[at:]],
                          {**expected, "op": "synthetic-click-1"}))

    case(["owner-direction", "undo", "--from-app"], [words], id=words)
    for key in ("latest_window_h", "session_binding", "trust", "untrust"):
        case(["owner-direction", "set", "--from-app"], [key, words], key=key, value=words)
    for verb in ("get", "set", "remove", "restore", "import"):
        prefix = ["ledger", verb, "--json", "--project=synthetic"]
        if verb != "get":
            prefix.append("--from-app")
        flags = {
            "set": [f"--title={words}", f"--note={words}", "--if-state=ready"],
            "remove": ["--children=move-up", f"--note={words}"],
            "import": ["--dry-run"],
        }.get(verb, [])
        field = "file" if verb == "import" else "id"
        case([*prefix, *flags], [words], **{field: words})
        if flags:
            case(prefix, [words], **{field: words})
    for verb in ("hide", "unhide"):
        case(["work", verb], [words], id=words)
    for verb in ("same", "not-same"):
        case(["work", verb], [words, words], row=words, commitment=words)
    case(["work", "check", "--json"], [words], file=words)
    case(["search", "--mode", "hybrid", "--limit", "20"], [words], query=words)
    case(["search", "--mode", "hybrid", "--limit", "20", "--project=synthetic",
          "--since=7d", "--kind=episode", "--harness=codex", "--via=t3"], [words], query=words)
    for hops, limit in (("1", "20"), ("2", "40")):
        case(["graph", "--hops", hops, "--limit", limit], [words], id=words)

    seen = []

    def handler(args):
        seen.append(vars(args))
        return 0

    parser = cli.build_parser()

    def replace_handlers(p):
        p.set_defaults(func=handler)
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    replace_handlers(child)

    replace_handlers(parser)
    with patch.object(cli, "build_parser", return_value=parser):
        for argv, expected in cases:
            assert cli.main(argv) == 0, argv
            parsed = seen.pop()
            assert {key: parsed[key] for key in expected} == expected, argv

        # A flag-looking word cannot opt a caller into the app path.
        for verb, params, field in (("rule", ["add"], "params"), ("note", ["set"], "text")):
            assert cli.main(["route", verb, *params, "--", words]) == 0
            parsed = seen.pop()
            assert parsed["from_app"] is False
            assert parsed[field] == ([words] if field == "params" else words)
