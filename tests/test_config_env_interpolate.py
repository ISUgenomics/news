"""Behaviour tests for the config_env_interpolate seed.

One test per contract line in the module docstring plus every edge case named in
docs/seeds/config-env-interpolate.md: missing variable, key path through a list
inside a mapping, empty default, escaped dollar, bare ``$VAR``, malformed
references, non-string scalars, unicode, and the rule that no resolved value
ever reaches a human-readable string.

Pure stdlib: no network, no stubs, no files.
"""

import pytest

from brief.lib.config_env_interpolate import (
    MissingConfigVar,
    interpolate_config,
    interpolate_config_report,
    interpolate_str,
)


# --- interpolate_str: the documented syntax --------------------------------


def test_str_substitutes_a_braced_reference():
    assert interpolate_str("token=${T}", {"T": "abc"}) == "token=abc"


def test_str_substitutes_several_references_in_one_string():
    out = interpolate_str("${A}://${B}/x", {"A": "https", "B": "host"})
    assert out == "https://host/x"


def test_str_leaves_a_bare_dollar_var_alone():
    # The deliberate choice: prices and shell fragments survive a config round-trip.
    assert interpolate_str("costs $5,000 and $HOME", {"HOME": "/root"}) == (
        "costs $5,000 and $HOME"
    )


def test_str_double_dollar_is_a_literal_dollar():
    assert interpolate_str("price: $$5", {}) == "price: $5"


def test_str_double_dollar_escapes_a_reference():
    # "$${T}" must survive as the literal text "${T}", not be substituted.
    assert interpolate_str("$${T}", {"T": "abc"}) == "${T}"


def test_str_default_is_used_when_the_name_is_absent():
    assert interpolate_str("${T:-fallback}", {}) == "fallback"


def test_str_default_is_used_when_the_value_is_empty():
    assert interpolate_str("${T:-fallback}", {"T": ""}) == "fallback"


def test_str_empty_default_resolves_to_the_empty_string():
    # Boundary fix: ${VAR:-} is a resolution, not a miss.
    assert interpolate_str("a${T:-}b", {}, strict=True) == "ab"


def test_str_default_is_literal_text_not_a_nested_reference():
    # The default stops at the first "}", so the text "${U" comes back literally
    # and the trailing "}" is ordinary text -- no nesting, no recursion.
    assert interpolate_str("${T:-${U}}", {"U": "u"}) == "${U}"


def test_str_default_text_is_literal_and_is_never_itself_interpolated():
    # "${U}" cannot survive inside a default -- the default stops at the first
    # "}" -- so the only observable form of "the default is literal text" is an
    # escape sequence: "$$" inside a default stays "$$", not "$". Without this
    # the documented no-recursion rule has no test that can go red.
    assert interpolate_str("${T:-$$5}", {}) == "$$5"
    assert interpolate_str("${T:-$$5}", {"T": ""}) == "$$5"


def test_str_resolved_value_is_inserted_verbatim():
    # A secret full of backslashes must not be read as a regex replacement
    # template: "\1" and "\g<0>" would otherwise expand or raise re.error.
    secret = "p\\1ss\\g<0>word\\"
    assert interpolate_str("${T}", {"T": secret}) == secret
    assert interpolate_str("${T:-x}", {"T": secret}) == secret


def test_str_default_value_is_inserted_verbatim():
    assert interpolate_str("${T:-a\\1b}", {}) == "a\\1b"


def test_str_present_but_empty_without_a_default_resolves_to_empty():
    assert interpolate_str("a${T}b", {"T": ""}) == "ab"


def test_str_value_containing_a_dollar_is_not_re_scanned():
    assert interpolate_str("${A}", {"A": "${B}", "B": "nope"}) == "${B}"


# --- interpolate_str: malformed text is not a reference --------------------


@pytest.mark.parametrize(
    "text",
    ["${}", "${ T }", "${2FA}", "${T", "${T-x}", "${a b}", "{T}"],
)
def test_str_malformed_references_are_left_verbatim_and_never_raise(text):
    assert interpolate_str(text, {}, strict=True) == text


# --- interpolate_str: strict vs non-strict ---------------------------------


def test_str_strict_raises_on_a_missing_variable():
    with pytest.raises(MissingConfigVar):
        interpolate_str("token=${T}", {})


def test_str_non_strict_leaves_the_reference_in_place():
    assert interpolate_str("token=${T}", {}, strict=False) == "token=${T}"


def test_str_non_strict_still_substitutes_what_it_can():
    out = interpolate_str("${A}/${B}", {"A": "ok"}, strict=False)
    assert out == "ok/${B}"


def test_str_error_carries_the_variable_and_the_supplied_path():
    with pytest.raises(MissingConfigVar) as excinfo:
        interpolate_str("${T}", {}, path="feeds[1].url")
    assert excinfo.value.variable == "T"
    assert excinfo.value.path == "feeds[1].url"


# --- MissingConfigVar ------------------------------------------------------


def test_missing_config_var_is_a_key_error():
    # Callers that already catch KeyError around config loading keep working.
    with pytest.raises(KeyError):
        interpolate_config({"a": "${T}"}, {})


def test_missing_config_var_message_names_the_variable_and_the_path():
    with pytest.raises(MissingConfigVar) as excinfo:
        interpolate_config({"feeds": [{"url": "${T}"}]}, {})
    message = str(excinfo.value)
    assert "T" in message
    assert "feeds[0].url" in message


def test_missing_config_var_message_is_readable_not_a_quoted_key_error():
    # KeyError's default __str__ is repr(args[0]); the message must not arrive
    # wrapped in quotes.
    with pytest.raises(MissingConfigVar) as excinfo:
        interpolate_str("${T}", {}, path="a.b")
    assert not str(excinfo.value).startswith("'")


def test_missing_config_var_at_the_top_level_has_an_empty_path():
    with pytest.raises(MissingConfigVar) as excinfo:
        interpolate_config("${T}", {})
    assert excinfo.value.path == ""
    assert "T" in str(excinfo.value)


def test_error_message_never_contains_a_resolved_value():
    # Boundary fix: names only. B is missing, A is a secret already substituted.
    env = {"A": "s3cret-token-value"}
    with pytest.raises(MissingConfigVar) as excinfo:
        interpolate_config({"url": "a=${A}&b=${B}"}, env)
    rendered = " ".join([str(excinfo.value), repr(excinfo.value)])
    assert "s3cret-token-value" not in rendered
    assert "a=" not in rendered  # nor the surrounding config text
    assert "B" in rendered


# --- interpolate_config: walking the structure -----------------------------


def test_config_substitutes_inside_nested_mappings_and_lists():
    data = {"feeds": [{"url": "http://h/?t=${T}"}, {"url": "plain"}]}
    out = interpolate_config(data, {"T": "abc"})
    assert out == {"feeds": [{"url": "http://h/?t=abc"}, {"url": "plain"}]}


def test_config_path_points_at_a_list_inside_a_mapping():
    # The whole reason this is a module and not a two-line regex.
    data = {"feeds": [{"url": "ok"}, {"url": "${T}"}]}
    with pytest.raises(MissingConfigVar) as excinfo:
        interpolate_config(data, {})
    assert excinfo.value.path == "feeds[1].url"


def test_config_path_for_a_nested_list_of_lists():
    data = {"a": [["x", "${T}"]]}
    with pytest.raises(MissingConfigVar) as excinfo:
        interpolate_config(data, {})
    assert excinfo.value.path == "a[0][1]"


def test_config_path_for_a_top_level_list():
    with pytest.raises(MissingConfigVar) as excinfo:
        interpolate_config(["${T}"], {})
    assert excinfo.value.path == "[0]"


def test_config_leaves_non_string_scalars_untouched():
    data = {"n": 5, "f": 1.5, "b": True, "none": None, "raw": b"${T}"}
    out = interpolate_config(data, {"T": "abc"})
    assert out == data
    assert out["n"] == 5 and isinstance(out["n"], int)
    assert out["b"] is True
    assert out["raw"] == b"${T}"


def test_config_does_not_interpolate_mapping_keys():
    out = interpolate_config({"${T}": "${T}"}, {"T": "abc"})
    assert out == {"${T}": "abc"}


def test_config_preserves_mapping_order():
    data = {"z": "1", "a": "2", "m": "3"}
    assert list(interpolate_config(data, {})) == ["z", "a", "m"]


def test_config_rebuilds_rather_than_mutating_the_input():
    data = {"feeds": [{"url": "${T}"}]}
    out = interpolate_config(data, {"T": "abc"})
    assert data == {"feeds": [{"url": "${T}"}]}
    assert out is not data
    assert out["feeds"] is not data["feeds"]


def test_config_keeps_tuples_as_tuples_and_lists_as_lists():
    out = interpolate_config({"t": ("${T}",), "l": ["${T}"]}, {"T": "x"})
    assert out["t"] == ("x",)
    assert isinstance(out["t"], tuple)
    assert out["l"] == ["x"]
    assert isinstance(out["l"], list)


def test_config_handles_empty_containers():
    assert interpolate_config({}, {}) == {}
    assert interpolate_config([], {}) == []
    assert interpolate_config({"a": {}, "b": []}, {}) == {"a": {}, "b": []}


def test_config_handles_a_bare_scalar_at_the_root():
    assert interpolate_config("${T}", {"T": "abc"}) == "abc"
    assert interpolate_config(7, {}) == 7


def test_config_non_string_keys_appear_in_the_path():
    with pytest.raises(MissingConfigVar) as excinfo:
        interpolate_config({3: "${T}"}, {})
    assert excinfo.value.path == "3"


def test_config_non_strict_leaves_unresolved_references_in_place():
    data = {"a": "${T}", "b": "${U}"}
    out = interpolate_config(data, {"U": "ok"}, strict=False)
    assert out == {"a": "${T}", "b": "ok"}


# --- unicode ---------------------------------------------------------------


def test_unicode_values_and_surrounding_text_survive():
    out = interpolate_config({"t": "café ${T} ☕"}, {"T": "naïve—ok"})
    assert out == {"t": "café naïve—ok ☕"}


def test_unicode_variable_names_are_not_references():
    # Names are ASCII; ${café} is punctuation, not a reference, so it survives.
    assert interpolate_str("${café}", {"café": "x"}) == "${café}"


# --- interpolate_config_report ---------------------------------------------


def test_report_returns_the_structure_and_an_empty_list_when_all_resolve():
    out, unresolved = interpolate_config_report({"a": "${T}"}, {"T": "x"})
    assert out == {"a": "x"}
    assert unresolved == []


def test_report_collects_variable_and_path_pairs():
    data = {"feeds": [{"url": "${T}"}], "x": "${U}"}
    out, unresolved = interpolate_config_report(data, {})
    assert out == data
    assert unresolved == [("T", "feeds[0].url"), ("U", "x")]


def test_report_never_raises_on_a_missing_variable():
    out, unresolved = interpolate_config_report("${T}", {})
    assert out == "${T}"
    assert unresolved == [("T", "")]


def test_report_records_one_entry_per_occurrence_in_document_order():
    out, unresolved = interpolate_config_report({"a": "${T}-${U}-${T}"}, {})
    assert out == {"a": "${T}-${U}-${T}"}
    assert unresolved == [("T", "a"), ("U", "a"), ("T", "a")]


def test_report_does_not_record_a_reference_that_had_a_default():
    out, unresolved = interpolate_config_report({"a": "${T:-d}"}, {})
    assert out == {"a": "d"}
    assert unresolved == []


def test_report_substitutes_what_it_can_alongside_what_it_cannot():
    out, unresolved = interpolate_config_report({"a": "${A}/${B}"}, {"A": "ok"})
    assert out == {"a": "ok/${B}"}
    assert unresolved == [("B", "a")]


# --- the module stays a seed ------------------------------------------------


def _module_source() -> str:
    import pathlib

    return pathlib.Path(interpolate_config.__globals__["__file__"]).read_text(
        encoding="utf-8"
    )


def _imported_roots() -> set[str]:
    """Root package of every import in the module, read from its real AST.

    A substring scan of the source is not enough: ``from os import environ``
    contains neither ``import os`` nor ``os.environ``, so a grep-style guard
    passes over a module that reads the process environment. The AST sees the
    module name whatever spelling was used.
    """
    import ast

    roots: set[str] = set()
    for node in ast.walk(ast.parse(_module_source())):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_module_imports_nothing_from_the_app():
    # Rule 3: nothing under lib/ may import from brief/.
    assert "brief" not in _imported_roots()


def test_module_never_imports_os_however_it_is_spelled():
    assert "os" not in _imported_roots()


def test_module_imports_only_the_stdlib_the_boundary_names():
    assert _imported_roots() <= {"__future__", "re", "collections", "typing"}


def test_resolution_ignores_the_real_process_environment():
    # The behavioural half of the guard: even with the name genuinely set in
    # this process, only the supplied mapping may be consulted.
    import os

    name = "BRIEF_SEED_ENV_ISOLATION_PROBE"
    previous = os.environ.get(name)
    os.environ[name] = "leaked-from-the-process"
    try:
        with pytest.raises(MissingConfigVar):
            interpolate_str("${%s}" % name, {})
        assert interpolate_str("${%s}" % name, {}, strict=False) == "${%s}" % name
        assert interpolate_str("${%s:-d}" % name, {}) == "d"
        assert interpolate_config({"a": "${%s}" % name}, {}, strict=False) == {
            "a": "${%s}" % name
        }
    finally:
        if previous is None:
            del os.environ[name]
        else:
            os.environ[name] = previous


def test_module_body_is_only_declarations_no_import_time_work():
    # Rule 3: no top-level side effects. Every statement at module level must
    # be a docstring, an import, an assignment, or a def/class -- a top-level
    # call (a file read, a clock read, a print) would fail this.
    import ast

    allowed = (
        ast.Import,
        ast.ImportFrom,
        ast.ClassDef,
        ast.FunctionDef,
        ast.Assign,
        ast.AnnAssign,
    )
    body = ast.parse(_module_source()).body
    assert isinstance(body[0], ast.Expr)  # the module docstring
    assert isinstance(body[0].value, ast.Constant)
    for statement in body[1:]:
        assert isinstance(statement, allowed), ast.dump(statement)


def test_env_has_no_default_so_the_caller_must_supply_one():
    with pytest.raises(TypeError):
        interpolate_config({"a": "${T}"})  # type: ignore[call-arg]
