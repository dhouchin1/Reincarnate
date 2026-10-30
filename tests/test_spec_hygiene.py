"""Kit v0.6 slice A: placeholder lint, donor quirks, endorsed-insecure notes, step 0 text."""

import pytest

from reincarnate import salvage

TLS_CONCERN = ("TLS verification disabled (`rejectUnauthorized: false`) for the app-ping "
               "endpoint — intentional for self-signed certs but disables certificate chain "
               "validation.")


@pytest.mark.parametrize("text", [
    "url and externalUrl ' 0 '", 'the label is " "', "{name}", "{{user}}", "<value>",
    "'<value>'", "TODO", "TBD later", "XXX", "Lorem ipsum dolor"])
def test_looks_placeholder_true(text):
    assert salvage.looks_placeholder(text), text


@pytest.mark.parametrize("text", [
    "{x, y}", "{location:{x:2}}", "Promise<T>", "200", "'a.png'", "tile url 'a.png'",
    "GET /items/{id}", "a <div> wrapper", "keeps a todo list", "the user's tile's size",
    "returns 'ok'", "",
    # real values seen in a live kit (v0.6 export): an empty default, a quoted number, string
    # building, and slots inside a format or a sentence describe patterns, not residue
    "the label is ''", "name is '0'", "name '', latitude 0", "base + ' (' + n + ')'",
    "'<statusText>: <status> (denied)'", "uses {name} in the title", "greets {{user}}",
    "shows <value> here"])
def test_looks_placeholder_false(text):
    assert not salvage.looks_placeholder(text), text


def _cap(store, **fields):
    data = {"summary": "A tool.", "capabilities": [
        {"name": "Tiles", "what": "Shows tiles on a board.", "files": ["t.py"], **fields}]}
    store.upsert_dossier("own", "repo", "https://github.com/own/repo", "abc", data)
    return store.list_capabilities()[0]


def test_spec_marks_placeholder_checks_and_values(store):
    cap = _cap(store)
    cap.acceptance = ["A tile's url and externalUrl ' 0 '", "A tile is 200px wide"]
    cap.key_values = [{"name": "title", "value": "{name}", "meaning": "heading"},
                      {"name": "w", "value": "200px", "meaning": ""}]
    spec = salvage.build_clean_room_spec(store, cap, target_slug="new:x",
                                         acceptance=cap.acceptance)
    mark = " [check this value: it looks like a placeholder]"
    assert f"- A tile's url and externalUrl ' 0 '{mark}\n" in spec
    assert f"- title: {{name}}{mark} — heading" in spec
    assert "- A tile is 200px wide\n" in spec and "- w: 200px\n" in spec
    assert salvage.placeholder_warnings(cap) == [
        "check: A tile's url and externalUrl ' 0 '", "key value title: {name}"]


def test_spec_renders_quirks_after_shapes_and_only_when_present(store):
    cap = _cap(store)
    cap.quirks = ["A migration drops the old owner field."]
    spec = salvage.build_clean_room_spec(store, cap, target_slug="new:x")
    head = "**Donor quirks (do not reproduce unless a requirement asks):**"
    assert f"{head}\n\n- A migration drops the old owner field.\n" in spec
    assert spec.index("**Behavior to implement:**") < spec.index(head)
    cap.quirks = []
    assert "Donor quirks" not in salvage.build_clean_room_spec(store, cap, target_slug="new:x")


def test_endorsed_insecure_note_becomes_avoid_with_opt_in_rule(store):
    cap = _cap(store, security_notes=TLS_CONCERN)
    spec = salvage.build_clean_room_spec(store, cap, target_slug="new:x")
    assert (f"- Avoid: {salvage.gate_text(TLS_CONCERN)} {salvage.SECURITY_OPT_IN}\n") in spec
    assert "off by default and visible in the UI" in spec and "Avoid: Avoid:" not in spec


def test_insecure_concern_from_dossier_is_not_double_avoided(store):
    data = {"summary": "x", "security": {"concerns": [
        "Tiles skip TLS verification: verify=False on the ping call."]},
        "capabilities": [{"name": "Tiles", "what": "Shows tiles.", "files": []}]}
    store.upsert_dossier("own", "repo", "https://github.com/own/repo", "abc", data)
    spec = salvage.build_clean_room_spec(store, store.list_capabilities()[0], target_slug="new:x")
    assert "- Avoid: Tiles skip TLS verification: verify=False on the ping call. Do not " \
           "reproduce." in spec
    assert "Avoid: Avoid:" not in spec


@pytest.mark.parametrize("note", [
    "Uses ssl.CERT_NONE for the context.", "Sets check_hostname = False.",
    "check_hostname=False on the client", "Calls _create_unverified_context().",
    "TLS verification disabled", "disables certificate validation"])
def test_new_insecure_patterns(note):
    assert salvage.is_insecure_check(note, negation=False), note
    assert salvage.avoid_insecure(note).startswith("Avoid: ")


def test_safe_tls_note_untouched(store):
    note = "Verifies TLS certificates for every outbound call."
    assert salvage.avoid_insecure(note) == note
    cap = _cap(store, security_notes=note)
    assert f"- {note}\n" in salvage.build_clean_room_spec(store, cap, target_slug="new:x")


def test_negation_escape_still_applies_to_checks_by_default():
    check = "Rejects the call when TLS verification is disabled"
    assert not salvage.is_insecure_check(check)
    assert salvage.is_insecure_check(check, negation=False)


def test_python_test_command_and_allowlist():
    assert salvage.TEST_COMMANDS["python"] == "python3 -m pytest -q"
    assert salvage.derive_test_cmd(["python"]) == "python3 -m pytest -q"
    allow = salvage.builder_bash_allowlist(["python"], "make test")
    for c in ("python3 -m pytest", ".venv/bin/python -m pytest", ".venv/bin/pip install",
              "python3 -m venv", "pip install", "uv add", "uv sync"):
        assert f"Bash({c}:*)" in allow, c
    assert "Bash(python -m pytest:*)" not in allow


def test_scaffold_python_venv_text_and_no_tsc():
    spec = salvage.build_scaffold_spec(["Python", "FastAPI"], "python3 -m pytest -q", ["python"])
    assert "- Python 3.10 or newer: declare it as `requires-python`" in spec
    assert "create `.venv` with that interpreter (for example `python3.12 -m venv .venv`" in spec
    assert "the kit's checks run `.venv/bin/python -m pytest -q` when it exists" in spec
    assert "under \"Layout\": the package or module name, the entry point" in spec
    assert "tsc" not in spec and "PointerEvent" not in spec


def test_scaffold_pins_the_versions_the_stack_names():
    spec = salvage.build_scaffold_spec(["Python 3.12", "Typer CLI"], "python3 -m pytest -q",
                                       ["python"])
    assert "- Python 3.12: declare it" in spec and "`python3.12 -m venv .venv`" in spec
    go = salvage.build_scaffold_spec(["Go", "gopkg.in/yaml.v3"], "go test ./...", ["go"])
    assert "- Go 1.22 or newer: set the `go` directive in `go.mod`" in go
    assert salvage.stack_versions(["Node.js 22", "Go 1.23", "Python"]) == {
        "node": "22", "go": "1.23"}


def test_scaffold_typescript_requires_tsc_in_npm_test():
    spec = salvage.build_scaffold_spec(["TypeScript", "React"], "npm test", ["node"],
                                       ui_tools=["@testing-library/react", "jsdom"])
    assert "`npm test`) runs `tsc --noEmit` before the unit tests" in spec
    assert "- `npm test` fails when a TypeScript file has a type error" in spec
    assert "shim `PointerEvent` and `ResizeObserver` (jsdom has neither)" in spec
    assert "virtual environment" not in spec


def test_scaffold_plain_javascript_has_no_tsc():
    spec = salvage.build_scaffold_spec(["JavaScript", "Express"], "npm test", ["node"])
    assert "tsc" not in spec
    mixed = salvage.build_scaffold_spec(["TypeScript", "Python"], "make test",
                                        ["node", "python"])
    assert "tsc --noEmit" in mixed and "- Node 20 or newer" in mixed
    assert "the `Makefile`'s Python suite runs `.venv/bin/python -m pytest -q`" in mixed


def test_component_test_line_has_the_jsdom_shim():
    line = salvage.component_test_line(["@testing-library/react", "jsdom"])
    assert "shim `PointerEvent` and `ResizeObserver` (jsdom has neither)" in line
    assert salvage.component_test_line([]) == ""


def test_long_stack_items_split_instead_of_clipping():
    from reincarnate import schema
    assert schema.clean_stack(salvage.split_stack(
        "Go (stdlib os/exec, log/slog, crypto/rand) + gopkg.in/yaml.v3")) == [
        "Go", "stdlib os/exec", "log/slog", "crypto/rand", "gopkg.in/yaml.v3"]
    assert schema.clean_stack(["SQLite (better-sqlite3 with FTS5 and sqlite-vec)"]) == [
        "SQLite", "better-sqlite3", "FTS5", "sqlite-vec"]
    short = ["Node (Fastify)", "Python 3.12"]
    assert schema.clean_stack(short) == short
    assert schema.clean_stack(["A very long framework name that keeps going past the limit"]) == [
        "A very long framework name that keeps"]
