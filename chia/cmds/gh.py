from __future__ import annotations

import json
import shlex
import uuid
import webbrowser
from pathlib import Path
from typing import Callable, ClassVar, Literal, Optional, Union

import anyio
import click
import yaml

from chia.cmds.cmd_classes import chia_command, option


class UnexpectedFormError(Exception):
    pass


Per = Union[Literal["directory"], Literal["file"]]


def report(*args: str) -> None:
    print("    ====", *args)


@click.group("gh", help="For working with GitHub")
def gh_group() -> None:
    pass


@chia_command(
    gh_group,
    name="test",
    # TODO: welp, yeah, help
    help="launch a test run in ci from the local commit",
    # short_help="helpy help",
    # help="""docstring help
    # and
    # more
    # lines
    #
    # blue
    # """,
)
class TestCMD:
    workflow_id: ClassVar[str] = "test.yml"
    owner: str = option("-o", "--owner", help="Owner of the repo", type=str, default="Chia-Network")
    repository: str = option("-r", "--repository", help="Repository name", type=str, default="chia-blockchain")
    ref: Optional[str] = option(
        "-f",
        "--ref",
        help="Branch or tag name (commit SHA not supported), if not specified will push HEAD to a temporary branch",
        type=str,
    )
    per: Per = option("-p", "--per", help="Per", type=click.Choice(["directory", "file"]), default="directory")
    only: Optional[Path] = option(
        "-o", "--only", help="Only run this item, a file or directory depending on --per", type=Path
    )
    duplicates: int = option("-d", "--duplicates", help="Number of duplicates", type=int, default=1)
    run_linux: bool = option("--run-linux/--skip-linux", help="Run on Linux", default=True)
    run_macos_intel: bool = option("--run-macos-intel/--skip-macos-intel", help="Run on macOS Intel", default=True)
    run_macos_arm: bool = option("--run-macos-arm/--skip-macos-arm", help="Run on macOS ARM", default=True)
    run_windows: bool = option("--run-windows/--skip-windows", help="Run on Windows", default=True)
    full_python_matrix: bool = option(
        "--full-python-matrix/--default-python-matrix", help="Run on all Python versions", default=False
    )

    async def run(self) -> None:
        await self.check_only()

        if self.ref is not None:
            await self.trigger_workflow(self.ref)
        else:
            task_uuid = uuid.uuid4()
            username = await self.get_username()
            temp_branch_name = f"tmp/{username}/{task_uuid}"

            await anyio.run_process(
                command=["git", "push", "origin", f"HEAD:{temp_branch_name}"], check=False, stdout=None, stderr=None
            )

            try:
                await self.trigger_workflow(temp_branch_name)
                for _ in range(10):
                    await anyio.sleep(1)

                    try:
                        report("looking for run")
                        run_url = await self.find_run(temp_branch_name)
                        report(f"run found at: {run_url}")
                    except UnexpectedFormError:
                        report("run not found")
                        continue

                    break
                else:
                    raise click.ClickException("Failed to find run url")
            finally:
                report(f"deleting temporary branch: {temp_branch_name}")
                process = await anyio.run_process(
                    command=["git", "push", "origin", "-d", temp_branch_name], check=False, stdout=None, stderr=None
                )
                if process.returncode != 0:
                    raise click.ClickException("Failed to dispatch workflow")
                report(f"temporary branch deleted: {temp_branch_name}")

            report(f"run url: {run_url}")
            webbrowser.open(run_url)

    async def check_only(self) -> None:
        if self.only is not None:
            import chia._tests

            test_path = Path(chia._tests.__file__).parent
            effective_path = test_path.joinpath(self.only)
            checks: dict[Per, Callable[[], bool]] = {"directory": effective_path.is_dir, "file": effective_path.is_file}
            check = checks[self.per]
            if not check():
                if effective_path.exists():
                    explanation = "wrong type"
                else:
                    explanation = "does not exist"
                message = f"expected requested --only to be a {self.per}, {explanation} at: {effective_path.as_posix()}"
                raise click.ClickException(message)

    async def trigger_workflow(self, ref: str) -> None:
        def input_arg(name: str, value: object, cond: bool = True) -> list[str]:
            dumped = yaml.safe_dump(value).partition("\n")[0]
            return [f"-f=inputs[{name}]={dumped}"] if cond else []

        # https://docs.github.com/en/rest/actions/workflows?apiVersion=2022-11-28#create-a-workflow-dispatch-event
        command = [
            "gh",
            "api",
            "--method=POST",
            "-H=Accept: application/vnd.github+json",
            "-H=X-GitHub-Api-Version: 2022-11-28",
            f"/repos/{self.owner}/{self.repository}/actions/workflows/{self.workflow_id}/dispatches",
            f"-f=ref={ref}",
            *input_arg("per", self.per),
            *input_arg("only", self.only, self.only is not None),
            *input_arg("duplicates", self.duplicates),
            *input_arg("run-linux", self.run_linux),
            *input_arg("run-macos-intel", self.run_macos_intel),
            *input_arg("run-macos-arm", self.run_macos_arm),
            *input_arg("run-windows", self.run_windows),
            *input_arg("full-python-matrix", self.full_python_matrix),
        ]
        report(f"running command: {shlex.join(command)}")
        process = await anyio.run_process(command=command, check=False, stdout=None, stderr=None)
        if process.returncode != 0:
            raise click.ClickException("Failed to dispatch workflow")
        report(f"workflow triggered on branch: {ref}")

    async def find_run(self, ref: str) -> str:
        # https://docs.github.com/en/rest/actions/workflow-runs?apiVersion=2022-11-28#list-workflow-runs-for-a-workflow
        command = [
            "gh",
            "api",
            "--method=GET",
            "-H=Accept: application/vnd.github+json",
            "-H=X-GitHub-Api-Version: 2022-11-28",
            f"-f=branch={ref}",
            f"/repos/{self.owner}/{self.repository}/actions/workflows/{self.workflow_id}/runs",
        ]
        report(f"running command: {shlex.join(command)}")
        process = await anyio.run_process(command=command, check=False, stderr=None)
        if process.returncode != 0:
            raise click.ClickException("Failed to query workflow runs")

        response = json.loads(process.stdout)
        runs = response["workflow_runs"]
        try:
            [run] = runs
        except ValueError:
            raise UnexpectedFormError(f"expected 1 run, got: {len(runs)}")

        url = run["html_url"]

        assert isinstance(url, str), f"expected url to be a string, got: {url!r}"
        return url

    async def get_username(self) -> str:
        # https://docs.github.com/en/rest/users/users?apiVersion=2022-11-28#get-the-authenticated-user
        process = await anyio.run_process(
            command=[
                "gh",
                "api",
                "--method=GET",
                "-H=Accept: application/vnd.github+json",
                "-H=X-GitHub-Api-Version: 2022-11-28",
                "/user",
            ],
            check=False,
            stderr=None,
        )
        if process.returncode != 0:
            raise click.ClickException("Failed to get username")

        response = json.loads(process.stdout)
        username = response["login"]
        assert isinstance(username, str), f"expected username to be a string, got: {username!r}"
        return username
