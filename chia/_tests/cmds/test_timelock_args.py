from __future__ import annotations

import click
from click.testing import CliRunner

from chia.cmds.cmds_util import timelock_args
from chia.wallet.conditions import ConditionValidTimes


def test_timelock_args() -> None:
    @click.command()
    @timelock_args
    def test_cmd(condition_valid_times: ConditionValidTimes) -> None:
        print(condition_valid_times.min_time)
        print(condition_valid_times.max_time)

    runner = CliRunner()

    result = runner.invoke(
        test_cmd,
        [
            "--valid-at",
            "0",
            "--expires-at",
            "0",
        ],
        catch_exceptions=False,
    )

    assert "0\n0\n" == result.output

    result = runner.invoke(
        test_cmd,
        [
            "--valid-at",
            "4294967295",
            "--expires-at",
            "4294967295",
        ],
        catch_exceptions=False,
    )

    assert "4294967295\n4294967295\n" == result.output

    result = runner.invoke(
        test_cmd,
        [],
        catch_exceptions=False,
    )

    assert "None\nNone\n" == result.output
