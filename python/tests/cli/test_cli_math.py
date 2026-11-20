from __future__ import annotations

import json

import pytest
from clitestkit import Cli, cli, write_draft  # noqa: F401 - cli is a fixture


def test_math_warnings_before_signing(cli: Cli) -> None:
    draft = write_draft(cli.cwd / "paper")
    (draft / "sci.json").write_text(r'{"title":"Example","abstract":"The angle is $\theta$."}')
    (draft / "README.md").write_text(
        "# Estimate\n\nThe ratio is $\\frac{a}{b$.\n\n\\[\nx^2\n\\]\n\n$$\n{x + y\n$$\n"
    )
    run = cli("check", str(draft), "--json")
    assert run.code == 0
    warnings = run.json["warnings"]
    assert len(warnings) == 4
    assert "sci.json /abstract" in warnings[0] and r"\\theta" in warnings[0]
    assert "README.md:3" in warnings[1] and "closing }" in warnings[1]
    assert "README.md:5" in warnings[2] and "$$" in warnings[2]
    assert "README.md:9" in warnings[3] and "closing }" in warnings[3]
    plain = cli("check", str(draft))
    assert plain.code == 0 and all(w in plain.stderr for w in warnings)
    assert cli("keygen").code == 0
    assert cli("sign", str(draft), "--no-timestamp").code == 0


@pytest.mark.parametrize("command", ["nu", "rho", "beta", "frac", "textbf", "binom", "forall"])
def test_json_escape_hint_even_if_the_title_fails_protocol(cli: Cli, command: str) -> None:
    draft = write_draft(cli.cwd / "paper")
    (draft / "sci.json").write_text(
        r'{"title":"$\COMMAND$","abstract":"Text"}'.replace("COMMAND", command)
    )
    run = cli("check", str(draft), "--json")
    assert run.code == 3 and run.json["errors"]
    assert any("/title" in w and "\\\\" + command in w for w in run.json["warnings"])


def test_math_literals_and_correctly_serialized_tex_are_quiet(cli: Cli) -> None:
    draft = write_draft(cli.cwd / "paper")
    (draft / "sci.json").write_text(
        json.dumps({"title": r"$\theta$", "abstract": "$eta$. Pay $5.\nNext week pay $10."})
    )
    (draft / "README.md").write_text(
        r"""Pay $5 or $10. The set is $\{x\}$ and a dollar is \$.

Example `$a{$` and multiline ``code
$a{$ ` x`` follow.

> ~~~tex
> \[
> $x{$
> ~~~

    \[
    $a{$

$$
\frac{1 % an ignored }
}{2}
$$
"""
    )
    run = cli("check", str(draft), "--json")
    assert run.code == 0 and run.json["warnings"] == []
